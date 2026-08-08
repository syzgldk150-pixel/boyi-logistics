"""
Fetch waybill info from Ronghui TMS via Playwright for N8N usage.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, Optional, Tuple

from browser_manager import TMSBrowserAuth, launch_browser
from shared_login import load_named_accounts, resolve_primary_credentials

BASE_URL = "https://tms.ronghuiwl.com"
HOME_URL = f"{BASE_URL}/module/index?mv=index"

DEFAULT_CONFIG_PATH = os.environ.get(
    "CONFIG_PATH",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json"),
)
DEFAULT_TIMEOUT_MS = 30_000
DEFAULT_ACTION_DELAY_SEC = 1.0
DEFAULT_BATCH_SIZE = 0
DEFAULT_MAX_WORKERS = 1
_ACTION_DELAY_SEC = DEFAULT_ACTION_DELAY_SEC

XPATH_MENU_CUSTOMER_SERVICE = (
    '//ul/li/a/span[normalize-space(.)="\u5ba2\u670d\u7ba1\u7406"]'
)
XPATH_MENU_TRACKING_FALLBACK = (
    '//ul//li//a/span[normalize-space(.)="\u5feb\u4ef6\u8ddf\u8e2a"]'
)

XPATH_TRACKING_IFRAME = (
    "//div[contains(@id, 'mini-') and contains(@id, '$body$')][not(contains(@style,'display'))]"
    "//iframe[starts-with(@name, 'mini-iframe-') and contains(@src, '/widget/home?authenticationKey=')]"
)
XPATH_BILL_TEXTAREA = (
    "//fieldset[@class=\"ux-fieldset\"]//span//textarea"
    "[@placeholder=\"\u591a\u4e2a\u5355\u53f7\u8bf7\u4f7f\u7528\u56de\u8f66\u7b26\u5206\u9694\"]"
)
XPATH_TRACK_IFRAME = "//iframe[@class=\"trackIframe\"]"
XPATH_TAB_WAYBILL = "//span[normalize-space(.)=\"\u8fd0\u5355\u4fe1\u606f\"]"

QUERY_BUTTON_XPATHS = (
    '//a[.//span[normalize-space(.)="\u67e5\u8be2"]]',
    '//a[normalize-space(.)="\u67e5\u8be2"]',
    '//span[normalize-space(.)="\u67e5\u8be2"]/ancestor::a[1]',
    '//a[.//span[normalize-space(.)="\u641c\u7d22"]]',
    '//a[normalize-space(.)="\u641c\u7d22"]',
)

LABEL_MAP: Dict[str, List[str]] = {
    "\u8d27\u7269\u540d\u79f0": ["\u8d27\u7269\u540d\u79f0"],
    "\u5305\u88c5\u7c7b\u578b": ["\u5305\u88c5\u7c7b\u578b"],
    "\u6d3e\u9001\u65b9\u5f0f": ["\u6d3e\u9001\u65b9\u5f0f"],
    "\u4ef6\u6570": ["\u4ef6\u6570"],
    "\u56de\u5355\u53f7": ["\u56de\u5355\u53f7"],
    "\u5b9e\u9645\u91cd\u91cf": ["\u5b9e\u9645\u91cd\u91cf"],
    "\u4f53\u79ef": ["\u4f53\u79ef"],
    "\u5907\u6ce8": ["\u5907\u6ce8"],
    "\u76ee\u7684\u7ad9\u70b9": ["\u76ee\u7684\u7ad9\u70b9", "\u76ee\u7684\u5730", "\u76ee\u7684\u7f51\u70b9"],
    "\u53d1\u8d27\u4eba": ["\u53d1\u8d27\u4eba", "\u53d1\u4ef6\u4eba", "\u5bc4\u4ef6\u4eba"],
    "\u53d1\u8d27\u7535\u8bdd": ["\u53d1\u8d27\u7535\u8bdd", "\u53d1\u4ef6\u4eba\u7535\u8bdd", "\u5bc4\u4ef6\u7535\u8bdd"],
    "\u53d1\u8d27\u5730\u5740": ["\u53d1\u8d27\u5730\u5740", "\u53d1\u4ef6\u5730\u5740", "\u5bc4\u4ef6\u5730\u5740"],
    "\u6536\u4ef6\u4eba": ["\u6536\u8d27\u4eba", "\u6536\u4ef6\u4eba"],
    "\u6536\u4ef6\u7535\u8bdd": ["\u6536\u4ef6\u7535\u8bdd", "\u6536\u4ef6\u4eba\u7535\u8bdd", "\u6536\u8d27\u7535\u8bdd"],
    "\u6536\u4ef6\u5730\u5740": ["\u6536\u4ef6\u5730\u5740", "\u8be6\u7ec6\u5730\u5740"],
    "\u7ed3\u7b97\u91cd\u91cf": ["\u7ed3\u7b97\u91cd\u91cf"],
    "\u4f53\u79ef\u91cd": ["\u4f53\u79ef\u91cd", "\u4f53\u79ef\u91cd\u91cf"],
    "\u8fd0\u8d39": ["\u8fd0\u8d39"],
    "\u652f\u4ed8\u7c7b\u578b": ["\u652f\u4ed8\u65b9\u5f0f", "\u652f\u4ed8\u7c7b\u578b"],
    "\u5230\u4ed8\u6b3e": ["\u5230\u4ed8\u6b3e"],
}

DECRYPT_BUTTON_TEXT = "\u89e3\u5bc6\u5ba2\u6237\u4fe1\u606f"
DECRYPT_BUTTON_ID = "#decryptBtn"

BILL_KEYS = (
    "bill_code",
    "billCode",
    "bill",
    "bill_no",
    "billNo",
    "main_bill",
    "mainBill",
    "master_bill",
    "masterBill",
    "\u4e3b\u5355\u53f7",
    "\u4e3b\u8fd0\u5355\u53f7",
)

_SPLIT_RE = re.compile(r"[,\s;]+")

JS_EXTRACT = r"""
(params) => {
  const labelMap = params && params.labelMap ? params.labelMap : {};
  const panel = Array.from(document.querySelectorAll(".mini-tabs-body > div"))
    .find(p => p.offsetParent !== null);
  if (!panel) return null;

  function compactText(value) {
    return String(value || "").replace(/\s+/g, "");
  }

  function normalizeRegion(value) {
    const text = String(value || "").trim();
    if (!text) return "";
    if (text.includes("|")) {
      const parts = text.split("|").map(part => part.trim()).filter(Boolean);
      return parts.length ? parts[parts.length - 1] : "";
    }
    return text;
  }

  function composeAddress(billData, provinceKey, cityKey, countyKey, addressKey) {
    if (!billData) return null;
    const detail = String(billData[addressKey] || "").trim();
    const detailCompact = compactText(detail);
    const parts = [billData[provinceKey], billData[cityKey], normalizeRegion(billData[countyKey])]
      .map(part => String(part || "").trim())
      .filter(Boolean);
    const prefixes = [];
    for (const part of parts) {
      const partCompact = compactText(part);
      if (prefixes.includes(part)) continue;
      if (partCompact && detailCompact.includes(partCompact)) continue;
      prefixes.push(part);
    }
    const value = prefixes.join("") + detail;
    return value || null;
  }

  function getValByLabel(label) {
    const labels = Array.from(panel.querySelectorAll(".form-labelfield-label"))
      .filter(el => el.textContent && el.textContent.trim() === label);
    if (!labels.length) return null;
    const view = labels[0].closest(".view.wdg");
    if (!view) return null;
    const input = view.querySelector("input,textarea,select");
    if (input) return input.value;
    const span = view.querySelector(".mini-textbox-input, .mini-buttonedit-input");
    if (span && span.value !== undefined) return span.value;
    const text = view.innerText.replace(label, "").trim();
    return text || null;
  }

  const result = {};
  Object.keys(labelMap).forEach((key) => {
    const labels = labelMap[key] || [];
    let value = null;
    for (const label of labels) {
      value = getValByLabel(label);
      if (value !== null && value !== undefined && value !== "") break;
    }
    result[key] = value;
  });

  const billCode = (document.querySelector("#billCode1") || {}).value || "";
  const billEntry = billCode && window.parent && window.parent.billMap ? window.parent.billMap[billCode] : null;
  const billData = billEntry && billEntry.billData ? billEntry.billData : null;
  if (billData) {
    const recipientAddress = composeAddress(
      billData,
      "ACCEPT_PROVINCE",
      "ACCEPT_CITY",
      "ACCEPT_COUNTY",
      "ACCEPT_MAN_ADDRESS"
    );
    if (recipientAddress) {
      result["收件地址"] = recipientAddress;
    }
  }
  return result;
}
"""


def _load_json_file(path: str) -> Any:
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def _resolve_credentials(
    config_path: str,
    *,
    username: str = "",
    password: str = "",
) -> Tuple[str, str]:
    _ = config_path
    return resolve_primary_credentials(username=username, password=password)


def _wait_xpath_visible(scope, xpath: str, *, timeout_ms: int = DEFAULT_TIMEOUT_MS):
    return scope.wait_for_selector(f"xpath={xpath}", state="visible", timeout=timeout_ms)


def _pause(scope, delay_sec: Optional[float] = None) -> None:
    if delay_sec is None:
        delay_sec = _ACTION_DELAY_SEC
    if delay_sec <= 0:
        return
    try:
        scope.wait_for_timeout(int(delay_sec * 1000))
    except Exception:
        time.sleep(delay_sec)


def _click_xpath(scope, xpath: str, *, timeout_ms: int = DEFAULT_TIMEOUT_MS) -> None:
    el = _wait_xpath_visible(scope, xpath, timeout_ms=timeout_ms)
    el.scroll_into_view_if_needed()
    el.click()
    _pause(scope)


def _fill_xpath(scope, xpath: str, value: str, *, timeout_ms: int = DEFAULT_TIMEOUT_MS) -> None:
    el = _wait_xpath_visible(scope, xpath, timeout_ms=timeout_ms)
    el.click()
    el.fill(value)
    _pause(scope)


def _is_visible(scope, xpath: str) -> bool:
    try:
        return scope.is_visible(f"xpath={xpath}")
    except Exception:
        return False


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
    parts = _SPLIT_RE.split(text.strip())
    codes = [_normalize_bill_code(part) for part in parts if part]
    return [code for code in codes if code]


def _normalize_item(raw: Any) -> Optional[Dict[str, str]]:
    if isinstance(raw, dict) and "json" in raw and isinstance(raw["json"], dict):
        raw = raw["json"]
    if not isinstance(raw, dict):
        return None
    for key in BILL_KEYS:
        if key in raw and raw[key]:
            bill = _normalize_bill_code(raw[key])
            if bill:
                return {"bill_code": bill}
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
        codes = _split_bill_codes(text)
        return [{"bill_code": code} for code in codes]
    if isinstance(raw, dict):
        for key in ("items", "data", "records", "rows"):
            if key in raw:
                return _coerce_items(raw.get(key))
        item = _normalize_item(raw)
        return [item] if item else []
    if isinstance(raw, list):
        items: List[Dict[str, str]] = []
        for entry in raw:
            if isinstance(entry, str):
                text = _normalize_bill_code(entry)
                if text:
                    items.append({"bill_code": text})
                continue
            item = _normalize_item(entry)
            if item:
                items.append(item)
        return items
    return []


def _coerce_account_keys(raw: Any) -> List[str]:
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
        if isinstance(parsed, list):
            return [str(item).strip() for item in parsed if str(item).strip()]
        return [part.strip() for part in re.split(r"[,;\n]+", text) if part.strip()]
    if isinstance(raw, list):
        return [str(item).strip() for item in raw if str(item).strip()]
    return []


def _normalize_account_entry(raw: Any) -> Optional[Tuple[str, str]]:
    if isinstance(raw, dict) and "json" in raw and isinstance(raw["json"], dict):
        raw = raw["json"]
    if not isinstance(raw, dict):
        return None
    username = None
    for key in ("username", "user", "uid", "account", "login", "operator_uid"):
        if key in raw and raw[key] is not None:
            username = raw[key]
            break
    password = None
    for key in ("password", "pwd", "pass", "operator_password"):
        if key in raw and raw[key] is not None:
            password = raw[key]
            break
    if not username or not password:
        return None
    return str(username).strip(), str(password).strip()


def _coerce_accounts(raw: Any) -> List[Tuple[str, str]]:
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
            return _coerce_accounts(parsed)
        accounts: List[Tuple[str, str]] = []
        for entry in [part.strip() for part in re.split(r"[,;\n]+", text) if part.strip()]:
            if ":" not in entry:
                continue
            user, pwd = entry.split(":", 1)
            user = user.strip()
            pwd = pwd.strip()
            if user and pwd:
                accounts.append((user, pwd))
        return accounts
    if isinstance(raw, dict):
        for key in ("accounts", "data", "records", "rows"):
            if key in raw:
                return _coerce_accounts(raw.get(key))
        account = _normalize_account_entry(raw)
        return [account] if account else []
    if isinstance(raw, list):
        accounts: List[Tuple[str, str]] = []
        for entry in raw:
            if isinstance(entry, str):
                text = entry.strip()
                if ":" in text:
                    user, pwd = text.split(":", 1)
                    user = user.strip()
                    pwd = pwd.strip()
                    if user and pwd:
                        accounts.append((user, pwd))
                continue
            account = _normalize_account_entry(entry)
            if account:
                accounts.append(account)
        return accounts
    return []


def _load_accounts_from_config(config_path: str, keys: List[str]) -> List[Tuple[str, str]]:
    _ = config_path
    return load_named_accounts(keys)


def _chunk_items(items: List[Dict[str, str]], batch_size: int) -> List[List[Dict[str, str]]]:
    if batch_size <= 0:
        return [items]
    chunks: List[List[Dict[str, str]]] = []
    for start in range(0, len(items), batch_size):
        chunks.append(items[start : start + batch_size])
    return chunks


def _open_tracking_menu(page, *, timeout_ms: int = DEFAULT_TIMEOUT_MS) -> None:
    if not _is_visible(page, XPATH_MENU_TRACKING_FALLBACK):
        _click_xpath(page, XPATH_MENU_CUSTOMER_SERVICE, timeout_ms=timeout_ms)

    if _is_visible(page, XPATH_MENU_TRACKING_FALLBACK):
        _click_xpath(page, XPATH_MENU_TRACKING_FALLBACK, timeout_ms=timeout_ms)
        return
    raise RuntimeError("Tracking menu not found")


def _get_tracking_frame(page, *, timeout_ms: int = DEFAULT_TIMEOUT_MS):
    deadline = time.time() + timeout_ms / 1000.0
    last_error: Optional[BaseException] = None
    while time.time() < deadline:
        try:
            iframe = page.query_selector(f"xpath={XPATH_TRACKING_IFRAME}")
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
    raise TimeoutError("Tracking iframe not found") from last_error


def _get_track_frame(parent_frame, *, timeout_ms: int = DEFAULT_TIMEOUT_MS):
    deadline = time.time() + timeout_ms / 1000.0
    last_error: Optional[BaseException] = None
    while time.time() < deadline:
        try:
            iframe = parent_frame.query_selector(f"xpath={XPATH_TRACK_IFRAME}")
            if iframe is None:
                parent_frame.wait_for_timeout(200)
                continue
            frame = iframe.content_frame()
            if frame is None:
                parent_frame.wait_for_timeout(200)
                continue
            return frame
        except BaseException as exc:
            last_error = exc
            parent_frame.wait_for_timeout(200)
    raise TimeoutError("trackIframe not found") from last_error


def _click_query_button(frame, *, timeout_ms: int = DEFAULT_TIMEOUT_MS) -> bool:
    for xpath in QUERY_BUTTON_XPATHS:
        if _is_visible(frame, xpath):
            _click_xpath(frame, xpath, timeout_ms=timeout_ms)
            return True
    return False


def _search_bill_code(frame, bill_code: str, *, timeout_ms: int = DEFAULT_TIMEOUT_MS) -> None:
    normalized = _normalize_bill_code(bill_code)
    _fill_xpath(frame, XPATH_BILL_TEXTAREA, normalized, timeout_ms=timeout_ms)
    if not _click_query_button(frame, timeout_ms=timeout_ms):
        el = _wait_xpath_visible(frame, XPATH_BILL_TEXTAREA, timeout_ms=timeout_ms)
        el.press("Enter")
        _pause(frame)


def _wait_bill_loaded(frame, bill_code: str, *, timeout_ms: int = DEFAULT_TIMEOUT_MS) -> None:
    script = """
    (bill) => {
      const text = document.body ? document.body.innerText || "" : "";
      return text.includes(bill);
    }
    """
    frame.wait_for_function(script, arg=bill_code, timeout=timeout_ms)


def _click_waybill_tab(frame, *, timeout_ms: int = DEFAULT_TIMEOUT_MS) -> None:
    _click_xpath(frame, XPATH_TAB_WAYBILL, timeout_ms=timeout_ms)


def _click_decrypt(frame) -> None:
    try:
        clicked = frame.evaluate(
            """
            () => {
              const btn = window.mini && window.mini.get && window.mini.get("decryptBtn");
              if (!btn) return false;
              const visible = typeof btn.getVisible === "function" ? btn.getVisible() : btn.visible !== false;
              const enabled = typeof btn.getEnabled === "function" ? btn.getEnabled() : btn.enabled !== false;
              if (!visible || !enabled) return false;
              if (typeof btn.doClick === "function") {
                btn.doClick();
                return true;
              }
              if (typeof btn.fire === "function") {
                btn.fire("click");
                return true;
              }
              return false;
            }
            """
        )
        if clicked:
            return
    except Exception:
        pass
    try:
        if frame.is_visible(DECRYPT_BUTTON_ID):
            frame.click(DECRYPT_BUTTON_ID)
            return
    except Exception:
        pass
    try:
        frame.click(f'xpath=//span[normalize-space(.)="{DECRYPT_BUTTON_TEXT}"]/ancestor::a[1]')
    except Exception:
        pass


def _extract_waybill_info(frame) -> Optional[Dict[str, Any]]:
    return frame.evaluate(JS_EXTRACT, {"labelMap": LABEL_MAP})


def _is_masked(value: Any) -> bool:
    if value is None:
        return True
    text = str(value)
    return "*" in text


def _wait_for_decrypt(
    frame,
    *,
    timeout_ms: int = DEFAULT_TIMEOUT_MS,
    retry_interval_ms: int = 600,
) -> Dict[str, Any]:
    deadline = time.time() + timeout_ms / 1000.0
    last: Dict[str, Any] = {}
    while time.time() < deadline:
        data = _extract_waybill_info(frame) or {}
        last = data
        name = data.get("\u6536\u4ef6\u4eba")
        phone = data.get("\u6536\u4ef6\u7535\u8bdd")
        if name and phone and not _is_masked(name) and not _is_masked(phone):
            return data
        frame.wait_for_timeout(retry_interval_ms)
    return last


def _normalize_output(data: Optional[Dict[str, Any]], bill_code: str) -> Dict[str, Any]:
    output: Dict[str, Any] = {"\u8fd0\u5355\u7f16\u53f7": bill_code}
    if not data:
        return output
    for key, value in data.items():
        if key == "\u5230\u8d27\u4ef6\u6570":
            continue
        output[key] = value
    return output


def run_flow(
    *,
    bill_code: str,
    items: Optional[List[Dict[str, str]]],
    username: str,
    password: str,
    config_path: str,
    headless: bool,
    slow_mo_ms: int,
    max_login_attempts: int,
    action_delay_sec: float,
    timeout_ms: int,
) -> List[Dict[str, Any]]:
    results: List[Dict[str, Any]] = []
    p = browser = context = page = None
    try:
        global _ACTION_DELAY_SEC
        _ACTION_DELAY_SEC = max(0.0, float(action_delay_sec))
        bill_items = _coerce_items(items)
        if not bill_items:
            single = _normalize_bill_code(bill_code)
            if not single:
                raise RuntimeError("Missing bill code")
            bill_items = [{"bill_code": single}]

        p, browser, context, page = launch_browser(headless=headless, slow_mo_ms=slow_mo_ms)

        uid, pwd = _resolve_credentials(config_path, username=username, password=password)
        auth = TMSBrowserAuth(max_attempts=max(1, int(max_login_attempts)), home_url=HOME_URL)
        auth.login(page, username=uid, password=pwd)
        page.goto(HOME_URL, wait_until="domcontentloaded", timeout=60_000)
        try:
            page.wait_for_load_state("networkidle", timeout=10_000)
        except Exception:
            pass

        _open_tracking_menu(page, timeout_ms=timeout_ms)

        frame = _get_tracking_frame(page, timeout_ms=timeout_ms)
        _wait_xpath_visible(frame, XPATH_BILL_TEXTAREA, timeout_ms=timeout_ms)

        for item in bill_items:
            bill = _normalize_bill_code(item.get("bill_code"))
            if not bill:
                continue

            try:
                frame = _get_tracking_frame(page, timeout_ms=timeout_ms)
                _search_bill_code(frame, bill, timeout_ms=timeout_ms)

                track_frame = _get_track_frame(frame, timeout_ms=timeout_ms)
                _wait_bill_loaded(track_frame, bill, timeout_ms=timeout_ms)
                _click_waybill_tab(track_frame, timeout_ms=timeout_ms)

                _click_decrypt(track_frame)
                data = _wait_for_decrypt(track_frame, timeout_ms=timeout_ms)
                results.append(_normalize_output(data, bill))
            except Exception as exc:
                results.append({"\u8fd0\u5355\u7f16\u53f7": bill, "error": str(exc)})

        return results
    finally:
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


def _error_results_for_batch(batch_items: List[Dict[str, str]], exc: BaseException) -> List[Dict[str, Any]]:
    message = str(exc)
    results: List[Dict[str, Any]] = []
    for item in batch_items:
        bill = _normalize_bill_code(item.get("bill_code"))
        if bill:
            results.append({"运单编号": bill, "error": message})
    if not results:
        results.append({"运单编号": "", "error": message})
    return results


def run_flow_parallel(
    *,
    bill_code: str,
    items: Optional[List[Dict[str, str]]],
    username: str,
    password: str,
    config_path: str,
    headless: bool,
    slow_mo_ms: int,
    max_login_attempts: int,
    action_delay_sec: float,
    timeout_ms: int,
    batch_size: int,
    max_workers: int,
    accounts: Optional[List[Tuple[str, str]]] = None,
) -> List[Dict[str, Any]]:
    bill_items = _coerce_items(items)
    if not bill_items:
        single = _normalize_bill_code(bill_code)
        if not single:
            raise RuntimeError("Missing bill code")
        bill_items = [{"bill_code": single}]

    batches = _chunk_items(bill_items, int(batch_size))
    max_workers = max(1, int(max_workers))
    accounts = accounts or []
    if max_workers <= 1 or len(batches) <= 1:
        return run_flow(
            bill_code=bill_code,
            items=bill_items,
            username=username,
            password=password,
            config_path=config_path,
            headless=headless,
            slow_mo_ms=slow_mo_ms,
            max_login_attempts=max_login_attempts,
            action_delay_sec=action_delay_sec,
            timeout_ms=timeout_ms,
        )

    if accounts:
        max_workers = min(max_workers, len(accounts))
    max_workers = max(1, min(max_workers, len(batches)))
    if max_workers <= 1:
        return run_flow(
            bill_code=bill_code,
            items=bill_items,
            username=username,
            password=password,
            config_path=config_path,
            headless=headless,
            slow_mo_ms=slow_mo_ms,
            max_login_attempts=max_login_attempts,
            action_delay_sec=action_delay_sec,
            timeout_ms=timeout_ms,
        )

    def _run_batch(batch_index: int, batch_items: List[Dict[str, str]]) -> Tuple[int, List[Dict[str, Any]]]:
        if accounts:
            batch_user, batch_pwd = accounts[batch_index % len(accounts)]
        else:
            batch_user, batch_pwd = username, password
        try:
            result = run_flow(
                bill_code="",
                items=batch_items,
                username=batch_user,
                password=batch_pwd,
                config_path=config_path,
                headless=headless,
                slow_mo_ms=slow_mo_ms,
                max_login_attempts=max_login_attempts,
                action_delay_sec=action_delay_sec,
                timeout_ms=timeout_ms,
            )
        except BaseException as exc:
            result = _error_results_for_batch(batch_items, exc)
        return batch_index, result

    futures = []
    results_by_index: List[Tuple[int, List[Dict[str, Any]]]] = []
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        for index, batch in enumerate(batches):
            futures.append(executor.submit(_run_batch, index, batch))
        for future in as_completed(futures):
            results_by_index.append(future.result())

    results_by_index.sort(key=lambda item: item[0])
    merged_results: List[Dict[str, Any]] = []
    for _, batch_results in results_by_index:
        merged_results.extend(batch_results)

    return merged_results


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Ronghui TMS waybill info (Playwright).")
    parser.add_argument("--bill-code", default="", help="Single bill code")
    parser.add_argument("--bill-codes", default="", help="Multiple bill codes, separated by comma/space/newline")
    parser.add_argument("--items-json", default="", help="JSON list/object")
    parser.add_argument("--items-file", default="", help="JSON file path")
    parser.add_argument("--username", default="", help="Username (optional)")
    parser.add_argument("--password", default="", help="Password (optional)")
    parser.add_argument("--config-path", default=DEFAULT_CONFIG_PATH, help="config.json path")
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE, help="batch size for parallel run")
    parser.add_argument("--max-workers", type=int, default=DEFAULT_MAX_WORKERS, help="max parallel workers")
    parser.add_argument("--accounts-json", default="", help="accounts list json")
    parser.add_argument("--account-keys", default="", help="config account keys, split by comma")
    parser.add_argument(
        "--headless",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Headless mode",
    )
    parser.add_argument("--slow-mo-ms", type=int, default=0, help="Slow motion in ms")
    parser.add_argument("--max-login-attempts", type=int, default=6, help="Login retry count")
    parser.add_argument("--timeout-ms", type=int, default=DEFAULT_TIMEOUT_MS, help="Timeout per step in ms")
    parser.add_argument(
        "--action-delay-sec",
        type=float,
        default=DEFAULT_ACTION_DELAY_SEC,
        help="Delay between actions in seconds",
    )
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)

    raw_items: Any = None
    if args.items_json:
        raw_items = str(args.items_json)
    elif args.items_file:
        raw_items = _load_json_file(str(args.items_file))
    elif args.bill_codes:
        raw_items = str(args.bill_codes)

    items = _coerce_items(raw_items)
    accounts = _coerce_accounts(args.accounts_json)
    if not accounts:
        account_keys = _coerce_account_keys(args.account_keys)
        accounts = _load_accounts_from_config(str(args.config_path), account_keys)

    results = run_flow_parallel(
        bill_code=str(args.bill_code),
        items=items,
        username=str(args.username),
        password=str(args.password),
        config_path=str(args.config_path),
        headless=bool(args.headless),
        slow_mo_ms=int(args.slow_mo_ms),
        max_login_attempts=int(args.max_login_attempts),
        action_delay_sec=float(args.action_delay_sec),
        timeout_ms=int(args.timeout_ms),
        batch_size=int(args.batch_size),
        max_workers=int(args.max_workers),
        accounts=accounts,
    )
    print(json.dumps(results, ensure_ascii=False))
    return 0


def _get_param(params: Optional[Dict[str, Any]], *keys: str, default: Any = None) -> Any:
    if not isinstance(params, dict):
        return default
    for key in keys:
        if key in params and params[key] is not None:
            return params[key]
    return default


def _coerce_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def run_once(params: Dict[str, Any]) -> Any:
    params = params or {}
    raw_items = _get_param(
        params,
        "items",
        "data",
        "records",
        "rows",
        "bill_codes",
        "billCodes",
        default=None,
    )
    if raw_items is None:
        raw_items = _get_param(params, "items_json", "itemsJson", "bill_codes_json", "billCodesJson", default="")

    items = _coerce_items(raw_items)
    if not items:
        single = _get_param(
            params,
            "bill_code",
            "billCode",
            "main_bill",
            "mainBill",
            "master_bill",
            "masterBill",
            "\u4e3b\u5355\u53f7",
            default="",
        )
        items = _coerce_items(single)

    config_path = str(_get_param(params, "config_path", "configPath", default=DEFAULT_CONFIG_PATH))
    accounts = _coerce_accounts(
        _get_param(params, "accounts", "accounts_json", "accountsJson", default=None)
    )
    if not accounts:
        account_keys = _coerce_account_keys(_get_param(params, "account_keys", "accountKeys", default=None))
        accounts = _load_accounts_from_config(config_path, account_keys)

    return run_flow_parallel(
        bill_code=str(_get_param(params, "bill_code", "billCode", default="")),
        items=items,
        username=str(_get_param(params, "username", default="")),
        password=str(_get_param(params, "password", default="")),
        config_path=config_path,
        headless=_coerce_bool(_get_param(params, "headless", default=True)),
        slow_mo_ms=int(_get_param(params, "slow_mo_ms", "slowMoMs", default=0)),
        max_login_attempts=int(_get_param(params, "max_login_attempts", "maxLoginAttempts", default=6)),
        action_delay_sec=float(
            _get_param(params, "action_delay_sec", "actionDelaySec", default=DEFAULT_ACTION_DELAY_SEC)
        ),
        timeout_ms=int(_get_param(params, "timeout_ms", "timeoutMs", default=DEFAULT_TIMEOUT_MS)),
        batch_size=int(_get_param(params, "batch_size", "batchSize", default=DEFAULT_BATCH_SIZE)),
        max_workers=int(_get_param(params, "max_workers", "maxWorkers", default=DEFAULT_MAX_WORKERS)),
        accounts=accounts,
    )


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
