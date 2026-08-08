from __future__ import annotations

import json
import logging
import os
import re
import shutil
import time
import uuid
from dataclasses import dataclass
from urllib.parse import urljoin
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from agent.tms_runtime.scripts.browser_manager import chromium_launch_kwargs

logger = logging.getLogger(__name__)

BASE_URL = "https://tms.ronghuiwl.com"
HOME_URL = f"{BASE_URL}/module/index?mv=index"
MENU_URL = f"{BASE_URL}/menuTreeExtend/loadMenu"

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_TEMP_ROOT = os.path.join(PROJECT_ROOT, "tmp", "split_complaint")

DEFAULT_TIMEOUT_MS = 30_000
DEFAULT_SCREENSHOT_SCROLL_ROUNDS = 8
DEFAULT_SCREENSHOT_SCROLL_WAIT_MS = 600
DEFAULT_EXCLUDED_SITES = ("邵阳操作场", "邵阳自提部")

TRACKING_MENU_TEXT = "快件跟踪"
TRACKING_PARENT_MENU_TEXT = "客服管理"
COMPLAINT_MENU_TEXT = "投诉方登记"
COMPLAINT_PARENT_MENU_TEXT = "投诉管理"

TRACKING_BILL_INPUT_XPATHS = (
    "//fieldset[@class='ux-fieldset']//span//textarea[@placeholder='多个单号请使用回车符分隔']",
    "//*[@id='BILL_CODE$text']",
    "//*[@name='BILL_CODE']",
)
QUERY_BUTTON_XPATHS = (
    "//a[.//span[normalize-space(.)='查询']]",
    "//a[normalize-space(.)='查询']",
    "//span[normalize-space(.)='查询']/ancestor::a[1]",
)
TRACK_IFRAME_XPATH = "//iframe[@class='trackIframe']"
CHILD_TAB_XPATHS = (
    "//span[normalize-space(.)='子单分布']",
    "//td[normalize-space(.)='子单分布']",
)
SCAN_TAB_XPATHS = (
    "//span[normalize-space(.)='扫描记录']",
    "//td[normalize-space(.)='扫描记录']",
)

COMPLAINT_SAVE_BUTTON_SELECTOR = "#saveBtn"
COMPLAINT_CANCEL_BUTTON_SELECTOR = "#cancelBtn"
COMPLAINT_UPLOAD_BUTTON_SELECTOR = "#addUploadFileBtn"
COMPLAINT_GRID_ID = "datagrid1"
COMPLAINT_PROBLEM_DESCRIPTION_FIELD = "REMARK"
DUPLICATE_COMPLAINT_TEXT = "同一单号同一类型同一网点仅支持上报一次"

_SPLIT_RE = re.compile(r"[,\s;]+")


@dataclass
class PageContext:
    tracking_url: str
    complaint_list_url: str
    complaint_add_url: str


@dataclass
class TrackingArtifacts:
    page1_path: str
    page2_path: str
    child_rows: List[Dict[str, str]]
    problem_bills: List[str]
    accused_site: str


@dataclass
class ComplaintSubmitResult:
    uploaded_files: List[str]
    saved: bool
    skipped: bool
    message: str


def _ts() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _log(stage: str, message: str) -> None:
    logger.info("[%s] stage=%s %s", _ts(), stage, message)


def _normalize_bill_code(value: Any) -> str:
    text = str(value).strip() if value is not None else ""
    if not text:
        return ""
    if text.startswith("="):
        text = text[1:].strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {"'", '"'}:
        text = text[1:-1].strip()
    return text


def _split_bill_codes(text: str) -> List[str]:
    return [item for item in (_normalize_bill_code(part) for part in _SPLIT_RE.split(text or "")) if item]


def _normalize_item(raw: Any) -> Optional[Dict[str, str]]:
    if isinstance(raw, dict) and "json" in raw and isinstance(raw["json"], dict):
        raw = raw["json"]
    if isinstance(raw, str):
        bill_code = _normalize_bill_code(raw)
        return {"bill_code": bill_code} if bill_code else None
    if not isinstance(raw, dict):
        return None
    for key in (
        "bill_code",
        "billCode",
        "bill",
        "bill_no",
        "billNo",
        "main_bill",
        "mainBill",
        "master_bill",
        "masterBill",
        "主单号",
    ):
        if raw.get(key):
            bill_code = _normalize_bill_code(raw[key])
            if bill_code:
                return {"bill_code": bill_code}
    return None


def _coerce_items(raw: Any) -> List[Dict[str, str]]:
    if raw is None:
        return []
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return []
        try:
            parsed = json.loads(text)
        except Exception:
            parsed = None
        if parsed is not None:
            return _coerce_items(parsed)
        return [{"bill_code": bill_code} for bill_code in _split_bill_codes(text)]
    if isinstance(raw, dict):
        for key in ("items", "data", "records", "rows"):
            if key in raw:
                return _coerce_items(raw.get(key))
        item = _normalize_item(raw)
        return [item] if item else []
    if isinstance(raw, list):
        items: List[Dict[str, str]] = []
        for entry in raw:
            item = _normalize_item(entry)
            if item:
                items.append(item)
        return items
    return []


def _get_param(params: Optional[Dict[str, Any]], *keys: str, default: Any = None) -> Any:
    if not isinstance(params, dict):
        return default
    for key in keys:
        if key in params and params[key] is not None:
            return params[key]
    return default


def _coerce_bool(value: Any, *, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def _parse_excluded_sites(raw: Any) -> List[str]:
    if raw is None:
        return list(DEFAULT_EXCLUDED_SITES)
    if isinstance(raw, (list, tuple, set)):
        sites = [str(item).strip() for item in raw if str(item).strip()]
        return sites or list(DEFAULT_EXCLUDED_SITES)
    text = str(raw).replace("\n", ",").replace(";", ",")
    sites = [item.strip() for item in text.split(",") if item.strip()]
    return sites or list(DEFAULT_EXCLUDED_SITES)


def _safe_json(resp) -> Any:
    try:
        return resp.json()
    except Exception:
        return None


def _walk_menu_nodes(nodes: Iterable[Dict[str, Any]], path: str = ""):
    for node in nodes or []:
        text = str(node.get("text") or "").strip()
        current_path = f"{path}/{text}" if path else text
        yield node, current_path
        yield from _walk_menu_nodes(node.get("children") or [], current_path)


def _resolve_widget_menu_url(session, leaf_text: str) -> str:
    resp = session.get(MENU_URL, timeout=20)
    resp.raise_for_status()
    payload = _safe_json(resp) or {}
    menu_data = payload.get("result", {}).get("data") or []
    for node, _path in _walk_menu_nodes(menu_data):
        text = str(node.get("text") or "").strip()
        url = str(node.get("url") or "").strip()
        if text != leaf_text or not url or "/widget/home?" not in url:
            continue
        return url if url.startswith("http") else f"{BASE_URL}{url}"
    raise RuntimeError(f"Failed to resolve menu URL for {leaf_text}")


def _resolve_add_url_from_html(html: str) -> str:
    patterns = (
        r"addMethodCs5[\s\S]{0,3000}?url\s*:\s*['\"]([^'\"]+)['\"]",
        r"addMethod[\s\S]{0,3000}?url\s*:\s*['\"]([^'\"]+)['\"]",
        r"function\s+add\w*\s*\([^)]*\)\s*\{[\s\S]{0,3000}?url\s*:\s*['\"]([^'\"]+)['\"]",
        r"openWindow\(\s*\{[\s\S]{0,3000}?url\s*:\s*['\"]([^'\"]+)['\"]",
    )
    for pattern in patterns:
        match = re.search(pattern, html)
        if not match:
            continue
        url = match.group(1).strip()
        if not url:
            continue
        return url if url.startswith("http") else f"{BASE_URL}{url}"
    raise RuntimeError("Failed to resolve complaint add URL from complaint list page")


def _resolve_page_context(session) -> PageContext:
    tracking_url = _resolve_widget_menu_url(session, TRACKING_MENU_TEXT)
    complaint_list_url = _resolve_widget_menu_url(session, COMPLAINT_MENU_TEXT)
    complaint_html = session.get(complaint_list_url, timeout=20).text
    complaint_add_url = _resolve_add_url_from_html(complaint_html)
    return PageContext(
        tracking_url=tracking_url,
        complaint_list_url=complaint_list_url,
        complaint_add_url=complaint_add_url,
    )


def _choose_accused_site(site_order: Sequence[str], excluded_sites: Sequence[str]) -> str:
    candidates = [site for site in site_order if site and site not in excluded_sites]
    if not candidates:
        return ""
    preferred = [site for site in candidates if site != "长沙分拨"]
    if preferred:
        return preferred[0]
    return candidates[0]


def _collect_problem_rows(
    child_rows: Sequence[Dict[str, str]],
    excluded_sites: Sequence[str],
    *,
    main_bill_code: str = "",
) -> List[Dict[str, str]]:
    result: List[Dict[str, str]] = []
    seen = set()
    for row in child_rows:
        site = str(row.get("site") or "").strip()
        bill = _normalize_bill_code(row.get("bill"))
        if not bill or bill in seen or site in excluded_sites:
            continue
        if main_bill_code and bill == main_bill_code:
            continue
        seen.add(bill)
        result.append({"bill": bill, "site": site})
    return result


def _collect_problem_bills(
    child_rows: Sequence[Dict[str, str]],
    excluded_sites: Sequence[str],
    *,
    main_bill_code: str = "",
) -> ComplaintSubmitResult:
    result = _collect_problem_rows(child_rows, excluded_sites, main_bill_code=main_bill_code)
    return [row["bill"] for row in result]


def _collect_problem_sites(
    child_rows: Sequence[Dict[str, str]],
    excluded_sites: Sequence[str],
    *,
    main_bill_code: str = "",
) -> List[str]:
    rows = _collect_problem_rows(child_rows, excluded_sites, main_bill_code=main_bill_code)
    sites: List[str] = []
    seen = set()
    for row in rows:
        site = row["site"]
        if site in seen:
            continue
        seen.add(site)
        sites.append(site)
    return sites


def _build_problem_description(problem_bills: Sequence[str]) -> str:
    return "\n".join(problem_bills)


def _cookiejar_to_playwright(session) -> List[Dict[str, Any]]:
    cookies: List[Dict[str, Any]] = []
    for cookie in session.cookies:
        item: Dict[str, Any] = {
            "name": cookie.name,
            "value": cookie.value,
            "path": cookie.path or "/",
        }
        domain = (cookie.domain or "").lstrip(".")
        if domain:
            item["domain"] = domain
        else:
            item["url"] = BASE_URL
        if cookie.expires:
            try:
                item["expires"] = int(cookie.expires)
            except Exception:
                pass
        cookies.append(item)
    return cookies


class HeadlessTMSBrowser:
    def __init__(self, session, *, headless: bool = True, slow_mo_ms: int = 0):
        self.session = session
        self.headless = headless
        self.slow_mo_ms = slow_mo_ms
        self._playwright = None
        self._browser = None
        self._context = None

    def __enter__(self):
        try:
            from playwright.sync_api import sync_playwright  # type: ignore
        except Exception as exc:
            raise RuntimeError("Missing dependency `playwright`; headless fallback is unavailable.") from exc

        self._playwright = sync_playwright().start()
        self._browser = self._playwright.chromium.launch(
            **chromium_launch_kwargs(headless=self.headless, slow_mo_ms=self.slow_mo_ms)
        )
        self._context = self._browser.new_context(viewport={"width": 1440, "height": 900})
        cookies = _cookiejar_to_playwright(self.session)
        if cookies:
            self._context.add_cookies(cookies)
        return self

    def __exit__(self, exc_type, exc, tb):
        if self._context is not None:
            self._context.close()
        if self._browser is not None:
            self._browser.close()
        if self._playwright is not None:
            self._playwright.stop()

    @property
    def context(self):
        if self._context is None:
            raise RuntimeError("Browser context is not initialized")
        return self._context

    def new_page(self):
        page = self.context.new_page()
        page.goto(HOME_URL, wait_until="domcontentloaded", timeout=60_000)
        return page


JS_SCROLL_ALL = r"""
(params) => {
  const direction = params && params.direction === "up" ? "up" : "down";

  function collectWindows(root) {
    const queue = [root];
    const seen = new Set();
    const out = [];
    while (queue.length) {
      const w = queue.shift();
      if (!w || seen.has(w)) continue;
      seen.add(w);
      out.push(w);
      let frames = [];
      try { frames = Array.from(w.document.querySelectorAll("iframe")); } catch (e) {}
      for (const frame of frames) {
        try {
          const dataSrc = (frame.getAttribute("data-src") || "").trim();
          const src = (frame.getAttribute("src") || "").trim();
          if ((!src || src === "about:blank") && dataSrc) {
            frame.setAttribute("src", dataSrc);
          }
        } catch (e) {}
        try { if (frame.contentWindow) queue.push(frame.contentWindow); } catch (e) {}
      }
    }
    return out;
  }

  function scrollElement(el) {
    if (!el) return;
    const target = direction === "up" ? 0 : el.scrollHeight || 0;
    try { el.scrollTop = target; } catch (e) {}
    try { el.scrollLeft = 0; } catch (e) {}
    try { el.dispatchEvent(new Event("scroll", { bubbles: true })); } catch (e) {}
  }

  function scrollWindow(w) {
    try {
      if (!w || !w.document) return;
    } catch (e) {
      return;
    }
    try { scrollElement(w.document.scrollingElement); } catch (e) {}
    try { scrollElement(w.document.documentElement); } catch (e) {}
    try { scrollElement(w.document.body); } catch (e) {}
    let elements = [];
    try { elements = Array.from(w.document.querySelectorAll("*")); } catch (e) {}
    for (const el of elements) {
      try {
        const style = w.getComputedStyle(el);
        if (!style) continue;
        const overflowY = style.overflowY;
        if (overflowY !== "auto" && overflowY !== "scroll") continue;
        if ((el.scrollHeight || 0) <= (el.clientHeight || 0) + 10) continue;
        scrollElement(el);
      } catch (e) {}
    }
  }

  const windows = collectWindows(window);
  for (const w of windows) {
    scrollWindow(w);
  }
  return true;
}
"""


JS_COLLECT_CHILD_DISTRIBUTION = r"""
() => {
  const fieldAliases = {
    bill: ["BILL_CODE", "bill_code", "billCode", "BILL_NO", "billNo", "SUB_BILL", "subBill"],
    site: ["SCAN_SITE", "scanSite", "SITE_NAME", "siteName", "SCAN_SITE_NAME", "scanSiteName"],
    type: ["SCAN_TYPE", "scanType", "TYPE_NAME", "typeName"],
    date: ["SCAN_DATE", "scanDate", "CREATE_DATE", "createDate"],
    desc: ["DESCRIPTION", "description", "SCAN_DESC", "scanDesc"],
  };
  const labelAliases = {
    bill: ["子单号", "运单号", "单号"],
    site: ["扫描网点", "网点"],
    type: ["扫描类型", "类型"],
    date: ["扫描时间", "时间"],
    desc: ["扫描记录", "描述", "说明", "备注"],
  };

  function norm(v) {
    return v == null ? "" : String(v).trim();
  }

  function primeLazyFrames(doc) {
    if (!doc) return;
    let frames = [];
    try { frames = Array.from(doc.querySelectorAll("iframe")); } catch (e) {}
    for (const frame of frames) {
      try {
        const dataSrc = norm(frame.getAttribute("data-src"));
        const src = norm(frame.getAttribute("src"));
        if ((!src || src === "about:blank") && dataSrc) {
          frame.setAttribute("src", dataSrc);
        }
      } catch (e) {}
    }
  }

  function collectWindows(root) {
    const queue = [root];
    const seen = new Set();
    const out = [];
    while (queue.length) {
      const w = queue.shift();
      if (!w || seen.has(w)) continue;
      seen.add(w);
      out.push(w);
      try { primeLazyFrames(w.document); } catch (e) {}
      let frames = [];
      try { frames = Array.from(w.document.querySelectorAll("iframe")); } catch (e) {}
      for (const frame of frames) {
        try { if (frame.contentWindow) queue.push(frame.contentWindow); } catch (e) {}
      }
    }
    return out;
  }

  function labelText(raw) {
    return norm(raw).replace(/\s+/g, "");
  }

  function fieldValue(row, keys) {
    for (const key of keys) {
      if (row && row[key] != null && row[key] !== "") return norm(row[key]);
      const lower = key.toLowerCase();
      if (row && row[lower] != null && row[lower] !== "") return norm(row[lower]);
    }
    return "";
  }

  function resolveRole(column) {
    const field = norm(column && column.field);
    if (field) {
      const lower = field.toLowerCase();
      for (const [role, aliases] of Object.entries(fieldAliases)) {
        if (aliases.some(alias => alias.toLowerCase() === lower)) return role;
      }
    }
    const header = labelText((column && (column.header || column.headerText || column.title || column.text || column.name)) || "");
    for (const [role, aliases] of Object.entries(labelAliases)) {
      if (aliases.some(alias => header.includes(alias))) return role;
    }
    return "";
  }

  function extractGrid(grid) {
    if (!grid) return [];
    let columns = [];
    let rows = [];
    try { columns = grid.getColumns() || []; } catch (e) {}
    try { rows = grid.getData() || []; } catch (e) {}
    if (!rows.length) return [];
    const roleColumns = {};
    for (const column of columns) {
      const role = resolveRole(column);
      if (role && !roleColumns[role]) roleColumns[role] = column;
    }
    const out = [];
    for (const row of rows) {
      const bill = roleColumns.bill ? norm(row[roleColumns.bill.field]) : fieldValue(row, fieldAliases.bill);
      const site = roleColumns.site ? norm(row[roleColumns.site.field]) : fieldValue(row, fieldAliases.site);
      const type = roleColumns.type ? norm(row[roleColumns.type.field]) : fieldValue(row, fieldAliases.type);
      const date = roleColumns.date ? norm(row[roleColumns.date.field]) : fieldValue(row, fieldAliases.date);
      const desc = roleColumns.desc ? norm(row[roleColumns.desc.field]) : fieldValue(row, fieldAliases.desc);
      if (!bill) continue;
      out.push({ bill, site, type, date, desc });
    }
    return out;
  }

  function headerIndexes(cells) {
    const normalized = cells.map(cell => labelText(cell));
    const result = { bill: -1, site: -1, type: -1, date: -1, desc: -1 };
    for (const [role, aliases] of Object.entries(labelAliases)) {
      for (let i = 0; i < normalized.length; i += 1) {
        if (aliases.some(alias => normalized[i].includes(alias))) {
          result[role] = i;
          break;
        }
      }
    }
    return result;
  }

  function extractTables(doc) {
    if (!doc) return [];
    const out = [];
    const tables = Array.from(doc.querySelectorAll("table"));
    for (const table of tables) {
      const rows = Array.from(table.querySelectorAll("tr"));
      let header = null;
      let startIndex = 0;
      for (let i = 0; i < rows.length; i += 1) {
        const cells = Array.from(rows[i].querySelectorAll("th,td")).map(td => norm(td.innerText || td.textContent || ""));
        if (!cells.length) continue;
        const idx = headerIndexes(cells);
        if (idx.bill >= 0 && idx.site >= 0) {
          header = idx;
          startIndex = i + 1;
          break;
        }
      }
      if (!header) continue;
      for (let i = startIndex; i < rows.length; i += 1) {
        const cells = Array.from(rows[i].querySelectorAll("th,td")).map(td => norm(td.innerText || td.textContent || ""));
        if (!cells.length) continue;
        const bill = header.bill >= 0 ? norm(cells[header.bill]) : "";
        const site = header.site >= 0 ? norm(cells[header.site]) : "";
        const type = header.type >= 0 ? norm(cells[header.type]) : "";
        const date = header.date >= 0 ? norm(cells[header.date]) : "";
        const desc = header.desc >= 0 ? norm(cells[header.desc]) : "";
        if (!bill) continue;
        out.push({ bill, site, type, date, desc });
      }
    }
    return out;
  }

  const orderedSites = [];
  const seenSites = new Set();
  const dedup = new Set();
  const rows = [];
  const windows = collectWindows(window);

  for (const w of windows) {
    try {
      if (w.mini && typeof w.mini.get === "function") {
        const ids = Array.from(w.document.querySelectorAll(".mini-datagrid")).map(el => el.id).filter(Boolean);
        for (const id of ids) {
          try {
            const grid = w.mini.get(id);
            for (const row of extractGrid(grid)) {
              const key = [row.bill, row.site, row.type, row.date, row.desc].join("|");
              if (dedup.has(key)) continue;
              dedup.add(key);
              rows.push(row);
              if (row.site && !seenSites.has(row.site)) {
                seenSites.add(row.site);
                orderedSites.push(row.site);
              }
            }
          } catch (e) {}
        }
      }
    } catch (e) {}

    try {
      for (const row of extractTables(w.document)) {
        const key = [row.bill, row.site, row.type, row.date, row.desc].join("|");
        if (dedup.has(key)) continue;
        dedup.add(key);
        rows.push(row);
        if (row.site && !seenSites.has(row.site)) {
          seenSites.add(row.site);
          orderedSites.push(row.site);
        }
      }
    } catch (e) {}
  }

  return {
    rows,
    site_order: orderedSites,
    frame_count: windows.length,
  };
}
"""


def _wait_network_idle(page, timeout_ms: int = 10_000) -> None:
    try:
        page.wait_for_load_state("networkidle", timeout=timeout_ms)
    except Exception:
        pass


def _goto(page, url: str, *, timeout_ms: int = 60_000) -> None:
    page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
    _wait_network_idle(page)


def _locator_count(scope, selector: str) -> int:
    try:
        return scope.locator(selector).count()
    except Exception:
        return 0


def _find_visible_text_locator(
    scope,
    selector: str,
    text: str,
    *,
    exact: bool = True,
    timeout_ms: int = DEFAULT_TIMEOUT_MS,
):
    deadline = time.time() + timeout_ms / 1000.0
    last_error: Optional[BaseException] = None
    while time.time() < deadline:
        try:
            locator = scope.locator(selector)
            count = locator.count()
            for index in range(count):
                item = locator.nth(index)
                try:
                    if not item.is_visible():
                        continue
                    item_text = (item.inner_text() or "").strip()
                    if exact and item_text != text:
                        continue
                    if not exact and text not in item_text:
                        continue
                    return item
                except Exception as exc:
                    last_error = exc
        except Exception as exc:
            last_error = exc
        scope.wait_for_timeout(200)
    raise TimeoutError(f"Failed to find visible text `{text}` with selector `{selector}`") from last_error


def _click_visible_text(
    scope,
    selector: str,
    text: str,
    *,
    exact: bool = True,
    timeout_ms: int = DEFAULT_TIMEOUT_MS,
):
    locator = _find_visible_text_locator(scope, selector, text, exact=exact, timeout_ms=timeout_ms)
    locator.scroll_into_view_if_needed()
    locator.click()
    return locator


def _wait_page_frame(page, predicate, *, description: str, timeout_ms: int = DEFAULT_TIMEOUT_MS):
    deadline = time.time() + timeout_ms / 1000.0
    last_error: Optional[BaseException] = None
    while time.time() < deadline:
        for frame in page.frames:
            try:
                if predicate(frame):
                    return frame
            except Exception as exc:
                last_error = exc
        page.wait_for_timeout(200)
    raise TimeoutError(f"Failed to locate {description}") from last_error


def _open_home_menu_frame(page, *, parent_text: str, leaf_text: str, frame_predicate, frame_description: str):
    _goto(page, HOME_URL)
    _click_visible_text(page, "span.menu-text", parent_text)
    page.wait_for_timeout(800)
    _click_visible_text(page, "span.menu-text", leaf_text)
    return _wait_page_frame(page, frame_predicate, description=frame_description)


def _open_tracking_entry_frame(page):
    return _open_home_menu_frame(
        page,
        parent_text=TRACKING_PARENT_MENU_TEXT,
        leaf_text=TRACKING_MENU_TEXT,
        frame_predicate=lambda frame: _locator_count(frame, "#BILL_CODE\\$text") > 0
        or _locator_count(frame, "[name='BILL_CODE']") > 0,
        frame_description="tracking entry frame",
    )


def _open_complaint_list_frame(page):
    return _open_home_menu_frame(
        page,
        parent_text=COMPLAINT_PARENT_MENU_TEXT,
        leaf_text=COMPLAINT_MENU_TEXT,
        frame_predicate=lambda frame: _locator_count(
            frame, "xpath=//span[contains(@class,'mini-button-text') and normalize-space(.)='新增']"
        )
        > 0
        and _locator_count(frame, "#saveBtn") == 0,
        frame_description="complaint list frame",
    )


def _open_complaint_form_frame(page):
    complaint_list_frame = _open_complaint_list_frame(page)
    _click_visible_text(complaint_list_frame, "span.mini-button-text", "新增")
    page.wait_for_timeout(800)
    return _wait_page_frame(
        page,
        lambda frame: _locator_count(frame, COMPLAINT_SAVE_BUTTON_SELECTOR) > 0
        and _locator_count(frame, f"#{COMPLAINT_GRID_ID}") > 0,
        description="complaint form frame",
    )


def _click_first_xpath(scope, xpaths: Sequence[str], *, timeout_ms: int = DEFAULT_TIMEOUT_MS) -> None:
    last_error: Optional[BaseException] = None
    for xpath in xpaths:
        try:
            locator = scope.locator(f"xpath={xpath}").first
            locator.wait_for(state="visible", timeout=timeout_ms)
            locator.scroll_into_view_if_needed()
            locator.click()
            return
        except Exception as exc:
            last_error = exc
    raise RuntimeError(f"Failed to click any selector: {xpaths}") from last_error


def _try_click_first_xpath(scope, xpaths: Sequence[str], *, timeout_ms: int = 2_000) -> bool:
    try:
        _click_first_xpath(scope, xpaths, timeout_ms=timeout_ms)
        return True
    except Exception:
        return False


def _fill_tracking_bill_input(scope, bill_code: str) -> None:
    last_error: Optional[BaseException] = None
    for xpath in TRACKING_BILL_INPUT_XPATHS:
        try:
            locator = scope.locator(f"xpath={xpath}").first
            locator.wait_for(state="visible", timeout=DEFAULT_TIMEOUT_MS)
            locator.click()
            locator.fill(bill_code)
            return
        except Exception as exc:
            last_error = exc
    raise RuntimeError("Failed to locate tracking bill input") from last_error


def _wait_track_frame(scope, *, timeout_ms: int = DEFAULT_TIMEOUT_MS):
    deadline = time.time() + timeout_ms / 1000.0
    last_error: Optional[BaseException] = None
    while time.time() < deadline:
        try:
            iframe = scope.locator(f"xpath={TRACK_IFRAME_XPATH}").first
            if iframe.count() == 0:
                scope.wait_for_timeout(200)
                continue
            element_handle = iframe.element_handle()
            if element_handle is None:
                scope.wait_for_timeout(200)
                continue
            frame = element_handle.content_frame()
            if frame is None:
                scope.wait_for_timeout(200)
                continue
            if not frame.url or frame.url == "about:blank":
                scope.wait_for_timeout(200)
                continue
            try:
                body_text = frame.locator("body").inner_text(timeout=1_000).strip()
            except Exception:
                body_text = ""
            if not body_text:
                scope.wait_for_timeout(200)
                continue
            return frame
        except Exception as exc:
            last_error = exc
            scope.wait_for_timeout(200)
    raise TimeoutError("Failed to locate trackIframe after search") from last_error


def _scroll_everything(frame, *, rounds: int = DEFAULT_SCREENSHOT_SCROLL_ROUNDS, wait_ms: int = DEFAULT_SCREENSHOT_SCROLL_WAIT_MS) -> None:
    frame.evaluate(JS_SCROLL_ALL, {"direction": "up"})
    frame.wait_for_timeout(200)
    for _ in range(max(1, rounds)):
        frame.evaluate(JS_SCROLL_ALL, {"direction": "down"})
        frame.wait_for_timeout(wait_ms)


def _collect_child_distribution(frame) -> Tuple[List[Dict[str, str]], List[str]]:
    result = frame.evaluate(JS_COLLECT_CHILD_DISTRIBUTION) or {}
    rows = result.get("rows") or []
    site_order = result.get("site_order") or []
    normalized_rows: List[Dict[str, str]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        bill = _normalize_bill_code(row.get("bill"))
        if not bill:
            continue
        normalized_rows.append(
            {
                "bill": bill,
                "site": str(row.get("site") or "").strip(),
                "type": str(row.get("type") or "").strip(),
                "date": str(row.get("date") or "").strip(),
                "desc": str(row.get("desc") or "").strip(),
            }
        )
    normalized_sites = [str(site).strip() for site in site_order if str(site).strip()]
    return normalized_rows, normalized_sites


def _save_frame_screenshot(frame, path: str) -> None:
    frame.locator("body").screenshot(path=path)


def _maybe_scroll_to_text(frame, text: str) -> None:
    if not text:
        return
    try:
        locator = frame.get_by_text(text, exact=False).first
        locator.scroll_into_view_if_needed()
        frame.wait_for_timeout(300)
    except Exception:
        return


def _distribution_site_marker(site_name: str) -> str:
    return f"\u3010{site_name}\u3011\u7f51\u70b9"


def _wait_distribution_site_frame(page, site_name: str, *, timeout_ms: int = DEFAULT_TIMEOUT_MS):
    if not site_name:
        raise RuntimeError("Site name is required for distribution screenshot")

    marker = f"【{site_name}】网点"
    deadline = time.time() + timeout_ms / 1000.0
    last_error: Optional[BaseException] = None
    while time.time() < deadline:
        for frame in page.frames:
            try:
                if not frame.url or "TYPE=%E5%AD%90%E5%8D%95%E8%BD%A8%E8%BF%B9" not in frame.url:
                    continue
                body = frame.locator("body")
                text = body.inner_text(timeout=1_000)
                if marker in text:
                    return frame
            except Exception as exc:
                last_error = exc
        page.wait_for_timeout(250)
    raise TimeoutError(f"Failed to locate distribution frame for site `{site_name}`") from last_error


def _wait_distribution_site_frame(page, site_name: str, *, timeout_ms: int = DEFAULT_TIMEOUT_MS):
    if not site_name:
        raise RuntimeError("Site name is required for distribution screenshot")

    marker = _distribution_site_marker(site_name)
    deadline = time.time() + timeout_ms / 1000.0
    last_error: Optional[BaseException] = None
    while time.time() < deadline:
        for frame in page.frames:
            try:
                if not frame.url or "TYPE=%E5%AD%90%E5%8D%95%E8%BD%A8%E8%BF%B9" not in frame.url:
                    continue
                body = frame.locator("body")
                text = body.inner_text(timeout=1_000)
                if marker in text:
                    return frame
            except Exception as exc:
                last_error = exc
        page.wait_for_timeout(250)
    raise TimeoutError(f"Failed to locate distribution frame for site `{site_name}`") from last_error


def _capture_tracking_artifacts(
    page,
    bill_code: str,
    bill_temp_dir: str,
    excluded_sites: Sequence[str],
) -> TrackingArtifacts:
    tracking_entry_frame = _open_tracking_entry_frame(page)
    _fill_tracking_bill_input(tracking_entry_frame, bill_code)
    _click_first_xpath(tracking_entry_frame, QUERY_BUTTON_XPATHS)
    track_frame = _wait_track_frame(tracking_entry_frame)

    _try_click_first_xpath(track_frame, SCAN_TAB_XPATHS)
    _scroll_everything(track_frame)
    page1_path = os.path.join(bill_temp_dir, "page1.png")
    _save_frame_screenshot(track_frame, page1_path)

    _click_first_xpath(track_frame, CHILD_TAB_XPATHS)
    _scroll_everything(track_frame)
    child_rows, site_order = _collect_child_distribution(track_frame)
    problem_bills = _collect_problem_bills(child_rows, excluded_sites, main_bill_code=bill_code)
    problem_sites = _collect_problem_sites(child_rows, excluded_sites, main_bill_code=bill_code)
    accused_site = _choose_accused_site(problem_sites, ())
    if not accused_site:
        raise RuntimeError("No accused site found after excluding configured sites")
    if not problem_bills:
        raise RuntimeError("No problem bills found after excluding configured sites")

    page2_path = os.path.join(bill_temp_dir, "page2.png")
    # page2 must capture the accused-site child frame so the screenshot shows
    # the site header instead of the full parent distribution page.
    site_frame = _wait_distribution_site_frame(page, accused_site)
    _save_frame_screenshot(site_frame, page2_path)

    return TrackingArtifacts(
        page1_path=page1_path,
        page2_path=page2_path,
        child_rows=child_rows,
        problem_bills=problem_bills,
        accused_site=accused_site,
    )


def _mini_set_value(page, component_id: str, value: str) -> bool:
    return bool(
        page.evaluate(
            """
            ({id, value}) => {
              const miniObj = window.mini && window.mini.get ? window.mini.get(id) : null;
              if (miniObj) {
                try { if (typeof miniObj.setValue === "function") miniObj.setValue(value); } catch (e) {}
                try { if (typeof miniObj.setText === "function") miniObj.setText(value); } catch (e) {}
                try { if (typeof miniObj.doValueChanged === "function") miniObj.doValueChanged(); } catch (e) {}
                return true;
              }
              const candidates = [
                document.getElementById(id + "$text"),
                document.getElementById(id),
                document.querySelector(`[name="${id}"]`),
              ].filter(Boolean);
              if (!candidates.length) return false;
              const el = candidates[0];
              el.focus();
              el.value = value;
              el.dispatchEvent(new Event("input", { bubbles: true }));
              el.dispatchEvent(new Event("change", { bubbles: true }));
              el.blur();
              return true;
            }
            """,
            {"id": component_id, "value": value},
        )
    )


def _mini_select_by_text(
    page,
    component_id: str,
    text: str,
    *,
    handler_name: str = "",
    timeout_ms: int = 10_000,
) -> Dict[str, Any]:
    deadline = time.time() + timeout_ms / 1000.0
    last_result: Dict[str, Any] = {}
    while time.time() < deadline:
        last_result = page.evaluate(
            """
            ({id, text, handlerName}) => {
              function norm(v) {
                return v == null ? "" : String(v).trim();
              }
              function firstValue(obj, keys) {
                for (const key of keys) {
                  if (obj && obj[key] != null && obj[key] !== "") return obj[key];
                }
                return "";
              }
              const comp = window.mini && window.mini.get ? window.mini.get(id) : null;
              if (!comp) {
                return { ok: false, reason: "component_not_found" };
              }
              let data = [];
              try { data = comp.getData ? (comp.getData() || []) : []; } catch (e) {}
              const textKeys = [
                comp.getTextField ? comp.getTextField() : "",
                "TEXT", "text", "NAME", "name", "TYPE_NAME", "CATEGORY", "CATEGORY_NAME", "COMPLAINT_TYPE",
                "SITE_NAME", "FULL_NAME", "LABEL", "title", "TITLE"
              ].filter(Boolean);
              const valueKeys = [
                comp.getValueField ? comp.getValueField() : "",
                "VALUE", "value", "ID", "id", "CODE", "TYPE_CODE", "CATEGORY_CODE", "SITE_CODE"
              ].filter(Boolean);
              const candidates = data.map((item) => {
                const itemText = norm(firstValue(item, textKeys));
                const itemValue = firstValue(item, valueKeys) || itemText;
                return { item, itemText, itemValue };
              }).filter((item) => item.itemText);
              const selected = candidates.find((item) => item.itemText === text)
                || candidates.find((item) => item.itemText.includes(text) || text.includes(item.itemText));
              if (!selected) {
                return { ok: false, reason: "option_not_found", available: candidates.map((item) => item.itemText) };
              }
              try { if (typeof comp.setValue === "function") comp.setValue(selected.itemValue); } catch (e) {}
              try { if (typeof comp.setText === "function") comp.setText(selected.itemText); } catch (e) {}
              try { if (typeof comp.doValueChanged === "function") comp.doValueChanged(); } catch (e) {}
              if (handlerName && typeof window[handlerName] === "function") {
                try { window[handlerName]({ selected: selected.item, sender: comp }); } catch (e) {
                  return { ok: false, reason: "handler_failed", error: String(e), available: candidates.map((item) => item.itemText) };
                }
              }
              return { ok: true, text: selected.itemText, value: selected.itemValue, available: candidates.map((item) => item.itemText) };
            }
            """,
            {"id": component_id, "text": text, "handlerName": handler_name},
        )
        if last_result.get("ok"):
            return last_result
        page.wait_for_timeout(300)
    return last_result


def _grid_row_count(page, grid_id: str) -> int:
    return int(
        page.evaluate(
            """
            (gridId) => {
              const grid = window.mini && window.mini.get ? window.mini.get(gridId) : null;
              if (!grid || typeof grid.getData !== "function") return 0;
              return (grid.getData() || []).length;
            }
            """,
            grid_id,
        )
        or 0
    )


def _wait_grid_rows(page, grid_id: str, *, min_rows: int, timeout_ms: int = 15_000) -> None:
    deadline = time.time() + timeout_ms / 1000.0
    while time.time() < deadline:
        if _grid_row_count(page, grid_id) >= min_rows:
            return
        page.wait_for_timeout(300)
    raise TimeoutError(f"Grid {grid_id} did not reach {min_rows} rows in time")


def _grid_select_row(page, grid_id: str, row_index: int) -> Dict[str, Any]:
    result = page.evaluate(
        """
        ({gridId, rowIndex}) => {
          const grid = window.mini && window.mini.get ? window.mini.get(gridId) : null;
          if (!grid || typeof grid.getData !== "function") {
            return { ok: false, reason: "grid_not_found" };
          }
          const rows = grid.getData() || [];
          const row = rows[rowIndex];
          if (!row) {
            return { ok: false, reason: "row_not_found", rowCount: rows.length };
          }
          try { if (typeof grid.select === "function") grid.select(row); } catch (e) {}
          return { ok: true, row };
        }
        """,
        {"gridId": grid_id, "rowIndex": row_index},
    )
    if not result.get("ok"):
        raise RuntimeError(f"Failed to select row {row_index} in {grid_id}: {result}")
    return result.get("row") or {}


def _grid_row_has_uploaded_file(page, grid_id: str, row_index: int) -> bool:
    return bool(
        page.evaluate(
            """
            ({gridId, rowIndex}) => {
              const grid = window.mini && window.mini.get ? window.mini.get(gridId) : null;
              if (!grid || typeof grid.getData !== "function") return false;
              const rows = grid.getData() || [];
              const row = rows[rowIndex];
              if (!row) return false;
              return Boolean(
                row.FILE_NAME || row.fileName || row.FILE_PATH || row.filePath || row.ATTACH_NAME || row.attachName
              );
            }
            """,
            {"gridId": grid_id, "rowIndex": row_index},
        )
    )


def _mini_component_url(page, component_id: str) -> str:
    return str(
        page.evaluate(
            """
            (id) => {
              const comp = window.mini && window.mini.get ? window.mini.get(id) : null;
              if (!comp) return "";
              try {
                if (typeof comp.getUrl === "function") return comp.getUrl() || "";
              } catch (e) {}
              return comp.url || "";
            }
            """,
            component_id,
        )
        or ""
    ).strip()


def _find_exception_site_row(session, lookup_url: str, site_name: str) -> Dict[str, Any]:
    if not lookup_url:
        raise RuntimeError("EXCEPTIONSITE_SIDE_CODE lookup URL is empty")

    url = lookup_url if lookup_url.startswith("http") else urljoin(f"{BASE_URL}/", lookup_url.lstrip("/"))
    resp = session.get(
        url,
        params={"pageSize": 100, "pageIndex": 0, "key": site_name},
        headers={
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Referer": f"{BASE_URL}/widget/home",
            "X-Requested-With": "XMLHttpRequest",
        },
        timeout=20,
    )
    resp.raise_for_status()
    payload = _safe_json(resp) or {}
    if isinstance(payload, dict) and payload.get("message") and not payload.get("data"):
        raise RuntimeError(f"EXCEPTIONSITE_SIDE_CODE lookup rejected: {str(payload.get('message')).strip()}")
    rows = payload.get("data") or payload.get("result", {}).get("data") or []
    if not isinstance(rows, list):
        rows = []

    exact = [row for row in rows if isinstance(row, dict) and str(row.get("SITE_NAME") or "").strip() == site_name]
    if exact:
        return exact[0]

    fuzzy = [row for row in rows if isinstance(row, dict) and site_name in str(row.get("SITE_NAME") or "").strip()]
    if fuzzy:
        return fuzzy[0]

    raise RuntimeError(f"Failed to find exception site row for `{site_name}`")


def _set_lookup_selected_row(page, component_id: str, row: Dict[str, Any], *, handler_name: str = "") -> Dict[str, Any]:
    result = page.evaluate(
        """
        ({id, row, handlerName}) => {
          const comp = window.mini && window.mini.get ? window.mini.get(id) : null;
          if (!comp) return { ok: false, reason: "component_not_found" };

          const valueField = comp.getValueField ? (comp.getValueField() || "SITE_CODE") : "SITE_CODE";
          const textField = comp.getTextField ? (comp.getTextField() || "SITE_NAME") : "SITE_NAME";
          const value = row[valueField] != null ? row[valueField] : (row.SITE_CODE || "");
          const text = row[textField] != null ? row[textField] : (row.SITE_NAME || "");

          try {
            if (comp.grid) {
              if (typeof comp.grid.setData === "function") comp.grid.setData([row]);
              if (typeof comp.grid.loadData === "function") comp.grid.loadData([row]);
              const gridRows = typeof comp.grid.getData === "function" ? (comp.grid.getData() || []) : [];
              if (typeof comp.grid.select === "function" && gridRows.length) comp.grid.select(gridRows[0]);
            }
          } catch (e) {}

          try { if (typeof comp.setValue === "function") comp.setValue(value); } catch (e) {}
          try { if (typeof comp.setText === "function") comp.setText(text); } catch (e) {}
          if (handlerName && typeof window[handlerName] === "function") {
            try {
              window[handlerName]({
                value,
                selected: row,
                sender: {
                  grid: {
                    getSelected: () => row,
                  },
                },
              });
            } catch (e) {
              return { ok: false, reason: "handler_failed", error: String(e), value, text };
            }
          }

          return { ok: true, value, text };
        }
        """,
        {"id": component_id, "row": row, "handlerName": handler_name},
    )
    if not result.get("ok"):
        raise RuntimeError(f"Failed to set lookup row for {component_id}: {result}")
    return result


def _dismiss_site_binding_prompts(form_frame, dialog_scope) -> None:
    texts = ("使用系统默认网点", "系统默认网点", "是", "确定")
    for _ in range(3):
        clicked = _dismiss_first_visible_text(form_frame, texts, timeout_ms=1_000)
        if not clicked and dialog_scope is not form_frame:
            clicked = _dismiss_first_visible_text(dialog_scope, texts, timeout_ms=1_000)
        if not clicked:
            return
        form_frame.wait_for_timeout(300)


def _dismiss_first_visible_text(page, texts: Sequence[str], *, timeout_ms: int = 1_500) -> Optional[str]:
    deadline = time.time() + timeout_ms / 1000.0
    while time.time() < deadline:
        for text in texts:
            try:
                locator = page.get_by_text(text, exact=False).first
                if locator.count() and locator.is_visible():
                    locator.click()
                    return text
            except Exception:
                continue
        page.wait_for_timeout(150)
    return None


def _find_first_visible_text(page, texts: Sequence[str]) -> Optional[str]:
    for text in texts:
        try:
            locator = page.get_by_text(text, exact=False).first
            if locator.count() and locator.is_visible():
                return text
        except Exception:
            continue
    return None


def _upload_grid_attachment(page, form_frame, row_index: int, file_path: str) -> str:
    _grid_select_row(form_frame, COMPLAINT_GRID_ID, row_index)
    with page.expect_response(lambda resp: "/file/upload" in resp.url, timeout=30_000) as upload_info:
        form_frame.locator("input[type=file]").first.set_input_files(file_path)
    response = upload_info.value
    if response.status >= 400:
        raise RuntimeError(f"Attachment upload failed with status {response.status}")
    deadline = time.time() + 20.0
    while time.time() < deadline:
        if _grid_row_has_uploaded_file(form_frame, COMPLAINT_GRID_ID, row_index):
            return os.path.basename(file_path)
        form_frame.wait_for_timeout(250)
    raise RuntimeError(f"Attachment row {row_index} did not update after upload")


def _fill_complaint_form(
    form_frame,
    *,
    bill_code: str,
    accused_site: str,
    problem_description: str,
    dialog_scope=None,
    session=None,
) -> None:
    dialog_scope = dialog_scope or form_frame

    if not _mini_set_value(form_frame, "BILL_CODE", bill_code):
        raise RuntimeError("Failed to fill BILL_CODE")
    form_frame.wait_for_timeout(300)

    result = _mini_select_by_text(form_frame, "APPEAL_TYPE", "差错处理")
    if not result.get("ok"):
        raise RuntimeError(f"Failed to select APPEAL_TYPE: {result}")

    result = _mini_select_by_text(form_frame, "CATEGORY", "违规操作类")
    if not result.get("ok"):
        raise RuntimeError(f"Failed to select CATEGORY: {result}")

    result = _mini_select_by_text(form_frame, "EXCEPTION_TYPE", "分批", handler_name="changeEXCEPTION_TYPE")
    if not result.get("ok"):
        raise RuntimeError(f"Failed to select EXCEPTION_TYPE: {result}")

    _wait_grid_rows(form_frame, COMPLAINT_GRID_ID, min_rows=3)

    if session is None:
        raise RuntimeError("Session is required for EXCEPTIONSITE_SIDE_CODE lookup")
    lookup_url = _mini_component_url(form_frame, "EXCEPTIONSITE_SIDE_CODE")
    site_row = _find_exception_site_row(session, lookup_url, accused_site)
    _set_lookup_selected_row(
        form_frame,
        "EXCEPTIONSITE_SIDE_CODE",
        site_row,
        handler_name="EXCEPTIONSITE_SIDE_CODE",
    )

    _dismiss_site_binding_prompts(form_frame, dialog_scope)

    if not _mini_set_value(form_frame, "EXCEPTION_MAN_PHONE", "1"):
        raise RuntimeError("Failed to fill EXCEPTION_MAN_PHONE")
    # `REMARK` is the visible "问题描述" field on the complaint form.
    if not _mini_set_value(form_frame, COMPLAINT_PROBLEM_DESCRIPTION_FIELD, problem_description):
        raise RuntimeError(f"Failed to fill {COMPLAINT_PROBLEM_DESCRIPTION_FIELD}")


def _close_complaint_form(page, form_frame) -> None:
    try:
        form_frame.locator(COMPLAINT_CANCEL_BUTTON_SELECTOR).first.click(force=True)
    except Exception:
        _dismiss_first_visible_text(page, ("取消", "关闭"), timeout_ms=1_500)


def _click_save_and_wait(page, form_frame, *, timeout_ms: int = 30_000) -> Tuple[str, Optional[Any]]:
    save_responses: List[Any] = []

    def _on_response(response) -> None:
        if "/dataOperation/saveTables" in response.url:
            save_responses.append(response)

    page.on("response", _on_response)
    try:
        form_frame.locator(COMPLAINT_SAVE_BUTTON_SELECTOR).first.click(force=True)
        deadline = time.time() + timeout_ms / 1000.0
        while time.time() < deadline:
            if save_responses:
                return "saved", save_responses[-1]

            duplicate_text = _find_first_visible_text(form_frame, (DUPLICATE_COMPLAINT_TEXT,))
            if not duplicate_text:
                duplicate_text = _find_first_visible_text(page, (DUPLICATE_COMPLAINT_TEXT,))
            if duplicate_text:
                _dismiss_first_visible_text(page, ("确定", "关闭"), timeout_ms=2_000)
                _close_complaint_form(page, form_frame)
                return "duplicate", None

            page.wait_for_timeout(200)
        raise TimeoutError("Timeout while waiting for complaint save response or duplicate warning")
    finally:
        try:
            page.remove_listener("response", _on_response)
        except Exception:
            try:
                page.off("response", _on_response)
            except Exception:
                pass


def _submit_complaint(
    page,
    session,
    *,
    bill_code: str,
    accused_site: str,
    problem_bills: Sequence[str],
    page1_path: str,
    page2_path: str,
) -> ComplaintSubmitResult:
    form_frame = _open_complaint_form_frame(page)
    form_frame.locator(COMPLAINT_SAVE_BUTTON_SELECTOR).first.wait_for(state="visible", timeout=DEFAULT_TIMEOUT_MS)

    _fill_complaint_form(
        form_frame,
        bill_code=bill_code,
        accused_site=accused_site,
        problem_description=_build_problem_description(problem_bills),
        dialog_scope=page,
        session=session,
    )

    uploaded = [
        _upload_grid_attachment(page, form_frame, 0, page1_path),
        _upload_grid_attachment(page, form_frame, 1, page2_path),
        _upload_grid_attachment(page, form_frame, 2, page2_path),
    ]

    save_status, response = _click_save_and_wait(page, form_frame, timeout_ms=30_000)
    if save_status == "duplicate":
        return ComplaintSubmitResult(
            uploaded_files=uploaded,
            saved=False,
            skipped=True,
            message="duplicate complaint exists; skipped",
        )

    if response is None:
        raise RuntimeError("Complaint save did not return a response")
    if response.status >= 400:
        raise RuntimeError(f"Complaint save failed with status {response.status}")

    payload = _safe_json(response)
    if isinstance(payload, dict):
        success = payload.get("success")
        code = payload.get("code")
        status = str(payload.get("status") or "").lower()
        if success is False:
            raise RuntimeError(f"Complaint save rejected: {payload}")
        if code not in (None, 0, "0") and status not in {"", "ok", "success", "200"}:
            raise RuntimeError(f"Complaint save returned unexpected payload: {payload}")

    _dismiss_first_visible_text(page, ("确定", "关闭", "知道了"), timeout_ms=2_000)
    return ComplaintSubmitResult(
        uploaded_files=uploaded,
        saved=True,
        skipped=False,
        message="success",
    )


def _run_single_bill(
    page,
    session,
    page_context: PageContext,
    *,
    bill_code: str,
    bill_temp_dir: str,
    excluded_sites: Sequence[str],
) -> Dict[str, Any]:
    tracking = _capture_tracking_artifacts(
        page,
        bill_code,
        bill_temp_dir,
        excluded_sites,
    )
    submit_result = _submit_complaint(
        page,
        session,
        bill_code=bill_code,
        accused_site=tracking.accused_site,
        problem_bills=tracking.problem_bills,
        page1_path=tracking.page1_path,
        page2_path=tracking.page2_path,
    )
    if submit_result.skipped:
        return {
            "bill_code": bill_code,
            "ok": True,
            "stage": "skipped",
            "message": submit_result.message,
            "accused_site": tracking.accused_site,
            "problem_bills": tracking.problem_bills,
            "uploaded_files": submit_result.uploaded_files,
            "saved": False,
            "skipped": True,
        }
    return {
        "bill_code": bill_code,
        "ok": True,
        "stage": "done",
        "message": submit_result.message,
        "accused_site": tracking.accused_site,
        "problem_bills": tracking.problem_bills,
        "uploaded_files": submit_result.uploaded_files,
        "saved": submit_result.saved,
        "skipped": False,
    }


def _cleanup_dir(path: str) -> None:
    if path and os.path.isdir(path):
        shutil.rmtree(path, ignore_errors=True)


def upload_split_complaints(
    session,
    bill_codes: Sequence[str],
    *,
    headless: bool = True,
    slow_mo_ms: int = 0,
    keep_artifacts: bool = False,
    temp_root: str = DEFAULT_TEMP_ROOT,
    excluded_sites: Sequence[str] = DEFAULT_EXCLUDED_SITES,
) -> List[Dict[str, Any]]:
    """Report split complaints with one authenticated browser; never callable as a target."""

    normalized_codes: List[str] = []
    seen: set[str] = set()
    for raw_code in bill_codes:
        bill_code = str(raw_code or "").strip()
        if not bill_code:
            raise ValueError("bill_codes contains an empty bill code")
        if bill_code in seen:
            raise ValueError(f"bill_codes contains duplicate bill code: {bill_code}")
        seen.add(bill_code)
        normalized_codes.append(bill_code)
    if not normalized_codes:
        return []

    batch_id = time.strftime("%Y%m%d%H%M%S") + "-" + uuid.uuid4().hex[:8]
    batch_dir = os.path.join(temp_root, batch_id)
    os.makedirs(batch_dir, exist_ok=True)
    page_context = _resolve_page_context(session)
    results: List[Dict[str, Any]] = []

    try:
        with HeadlessTMSBrowser(session, headless=headless, slow_mo_ms=slow_mo_ms) as browser:
            page = browser.new_page()
            for bill_code in normalized_codes:
                bill_dir = os.path.join(batch_dir, bill_code)
                os.makedirs(bill_dir, exist_ok=True)
                try:
                    _log("split_complaint", f"start bill={bill_code}")
                    raw_result = _run_single_bill(
                        page,
                        session,
                        page_context,
                        bill_code=bill_code,
                        bill_temp_dir=bill_dir,
                        excluded_sites=excluded_sites,
                    )
                    if raw_result.get("skipped"):
                        status = "duplicate"
                    elif raw_result.get("saved"):
                        status = "success"
                    else:
                        status = "failed"
                    results.append(
                        {
                            **raw_result,
                            "bill_code": bill_code,
                            "status": status,
                        }
                    )
                    _log("split_complaint", f"done bill={bill_code} status={status}")
                except Exception as exc:
                    logger.exception("split complaint failed for bill=%s", bill_code)
                    results.append(
                        {
                            "bill_code": bill_code,
                            "status": "failed",
                            "saved": False,
                            "skipped": False,
                            "error": f"{type(exc).__name__}: {exc}"[:500],
                        }
                    )
                finally:
                    if not keep_artifacts:
                        _cleanup_dir(bill_dir)
            page.close()
    finally:
        if not keep_artifacts:
            _cleanup_dir(batch_dir)
    return results
