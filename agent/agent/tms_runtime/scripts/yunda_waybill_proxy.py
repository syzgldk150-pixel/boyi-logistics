"""Raw same-origin proxy support for the Yunda waybill entry page."""

from __future__ import annotations

import base64
import re
from html import escape
from typing import Any
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

from agent.tms_runtime.session_broker import get_session_broker
from agent.tms_runtime.scripts.yunda_waybill_entry import (
    DEFAULT_TIMEOUT_SEC,
    ENTRY_INDEX_URL,
    YUNDA_INMS_ORIGIN,
    _auth_if_login_response,
)
from shared.manual_entry_contracts import (
    YUNDA_MANUAL_PROXY_ALLOWED_PREFIXES,
    canonical_manual_proxy_path,
)


ALLOWED_PATH_PREFIXES = YUNDA_MANUAL_PROXY_ALLOWED_PREFIXES
HOP_BY_HOP_REQUEST_HEADERS = {
    "accept-encoding",
    "authorization",
    "connection",
    "content-length",
    "cookie",
    "host",
    "proxy-authorization",
    "proxy-connection",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}
BLOCKED_RESPONSE_HEADERS = {
    "content-encoding",
    "content-length",
    "content-security-policy",
    "set-cookie",
    "transfer-encoding",
    "x-frame-options",
}
REWRITABLE_ATTRS = ("action", "href", "src")
ATTR_URL_RE = re.compile(
    r"(?P<prefix>\b(?:action|href|src)\s*=\s*)(?P<quote>['\"])(?P<url>[^'\"]*)(?P=quote)",
    flags=re.IGNORECASE,
)
UNQUOTED_ATTR_URL_RE = re.compile(
    r"(?P<prefix>\b(?:action|href|src)\s*=\s*)(?P<url>(?:https?://kyinms\.yunda56\.com)?/ky_inms/public/[^\s>'\"\)]*)",
    flags=re.IGNORECASE,
)
QUOTED_YUNDA_URL_RE = re.compile(
    r"(?P<quote>['\"])(?P<url>(?:https?://kyinms\.yunda56\.com)?/ky_inms/public/[^'\"]*)(?P=quote)",
    flags=re.IGNORECASE,
)
CSS_URL_RE = re.compile(
    r"(?P<prefix>\burl\(\s*)(?P<quote>['\"]?)(?P<url>(?:https?://kyinms\.yunda56\.com)?/ky_inms/public/[^'\"\)]*)(?P=quote)(?P<suffix>\s*\))",
    flags=re.IGNORECASE,
)
YUNDA_COST_VISIBILITY_HELPER = """
<style id="codex-yunda-cost-style">
.costInformation > div:has(.search_forms_dot) { display: block !important; }
.costInformation .search_forms_dot { display: flex !important; flex-wrap: wrap; }
.costInformation #isNewCost { display: block !important; width: 100%; }
.costInformation #classify_show_box { display: flex !important; flex-wrap: wrap; }
</style>
<script id="codex-yunda-cost-script">
(function () {
  function showYundaCostInfo() {
    var root = document.querySelector(".costInformation");
    if (!root) return;
    var form = root.querySelector(".search_forms_dot");
    if (form) {
      var holder = form.parentElement;
      if (holder) holder.style.setProperty("display", "block", "important");
      form.style.setProperty("display", "flex", "important");
    }
    var newCost = root.querySelector("#isNewCost");
    if (newCost) newCost.style.setProperty("display", "block", "important");
    var box = root.querySelector("#classify_show_box");
    if (box) box.style.setProperty("display", "flex", "important");
  }
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", showYundaCostInfo);
  } else {
    showYundaCostInfo();
  }
  try {
    new MutationObserver(showYundaCostInfo).observe(document.documentElement, {
      attributes: true,
      attributeFilter: ["class", "style"],
      childList: true,
      subtree: true
    });
  } catch (_) {}
  window.setInterval(showYundaCostInfo, 1000);
})();
</script>
"""
YUNDA_PREFILL_HELPER = """
<script id="codex-yunda-prefill-script">
(function () {
  if (window.codexManualPrefill && window.codexManualPrefill.yunda) return;
  window.codexManualPrefill = window.codexManualPrefill || {};
  var shipnowParentOrigin = window.location.origin === "https://www.boyi.homes"
    ? "https://boyi.homes"
    : window.location.origin;
  function clean(value) { return String(value == null ? "" : value).trim(); }
  function namesOf(spec) {
    var names = [];
    if (spec && spec.key) names.push(spec.key);
    if (spec && Array.isArray(spec.names)) names = names.concat(spec.names);
    return names.map(clean).filter(Boolean);
  }
  function setMiniValue(name, value) {
    try {
      if (!window.mini || typeof window.mini.get !== "function") return false;
      var control = window.mini.get(name);
      if (!control) return false;
      if (typeof control.setValue === "function") control.setValue(value);
      if (typeof control.setText === "function") control.setText(value);
      if (typeof control.doValueChanged === "function") control.doValueChanged();
      if (typeof control.fire === "function") control.fire("valuechanged");
      return true;
    } catch (_) {
      return false;
    }
  }
  function dispatchFieldEvents(element) {
    ["input", "change", "blur"].forEach(function (type) {
      try { element.dispatchEvent(new Event(type, { bubbles: true })); } catch (_) {}
    });
  }
  function setElementValue(element, value) {
    if (!element) return false;
    try {
      if (element.tagName === "SELECT") {
        var wanted = clean(value);
        Array.prototype.some.call(element.options || [], function (option) {
          if (clean(option.value) === wanted || clean(option.textContent) === wanted) {
            element.value = option.value;
            return true;
          }
          return false;
        });
      } else {
        element.value = value;
      }
      dispatchFieldEvents(element);
      return true;
    } catch (_) {
      return false;
    }
  }
  function selectorSafe(name) {
    if (window.CSS && typeof window.CSS.escape === "function") return window.CSS.escape(name);
    return String(name).replace(/["\\\\]/g, "\\\\$&");
  }
  function findDomField(name) {
    var escaped = selectorSafe(name);
    var selectors = [
      "#" + escaped,
      "[name=\"" + escaped + "\"]",
      "[data-field=\"" + escaped + "\"]",
      "[data-yunda-field=\"" + escaped + "\"]",
      "[placeholder*=\"" + escaped + "\"]"
    ];
    for (var i = 0; i < selectors.length; i += 1) {
      var found = document.querySelector(selectors[i]);
      if (found && /^(INPUT|TEXTAREA|SELECT)$/.test(found.tagName || "")) return found;
    }
    return null;
  }
  function findFieldNearLabel(name) {
    var labels = Array.prototype.slice.call(document.querySelectorAll("label,td,th,span,div"));
    for (var i = 0; i < labels.length; i += 1) {
      var label = labels[i];
      var text = clean(label.textContent).replace(/[：:*＊\\s]/g, "");
      var wanted = clean(name).replace(/[：:*＊\\s]/g, "");
      if (!wanted || text.indexOf(wanted) === -1) continue;
      var scope = label.closest("tr,.form-group,.search_forms_row,.mini-panel,.mini-tabs,.form_item") || label.parentElement;
      var field = scope && scope.querySelector("input,textarea,select");
      if (field) return field;
      var next = label.nextElementSibling;
      while (next) {
        if (/^(INPUT|TEXTAREA|SELECT)$/.test(next.tagName || "")) return next;
        field = next.querySelector && next.querySelector("input,textarea,select");
        if (field) return field;
        next = next.nextElementSibling;
      }
    }
    return null;
  }
  function fillSpec(spec) {
    var value = clean(spec && spec.value);
    if (!value) return { ok: true, skipped: true, key: clean(spec && spec.key) };
    var names = namesOf(spec);
    for (var i = 0; i < names.length; i += 1) {
      var name = names[i];
      if (setMiniValue(name, value)) return { ok: true, key: clean(spec.key || name), matched: name };
      var field = findDomField(name) || findFieldNearLabel(name);
      if (setElementValue(field, value)) return { ok: true, key: clean(spec.key || name), matched: name };
    }
    return { ok: false, key: clean(spec && spec.key) || names[0] || "unknown" };
  }
  function normalizeFields(payload) {
    var fields = payload && payload.fields;
    if (Array.isArray(fields)) return fields;
    if (fields && typeof fields === "object") {
      return Object.keys(fields).map(function (key) { return { key: key, names: [key], value: fields[key] }; });
    }
    return [];
  }
  function isVisible(element) {
    if (!element || !element.ownerDocument) return false;
    try {
      var style = window.getComputedStyle(element);
      if (!style || style.display === "none" || style.visibility === "hidden" || Number(style.opacity || 1) === 0) return false;
      return Boolean(element.offsetWidth || element.offsetHeight || element.getClientRects().length);
    } catch (_) {
      return false;
    }
  }
  function isEditableField(element) {
    if (!element || !/^(INPUT|TEXTAREA|SELECT)$/.test(element.tagName || "")) return false;
    if (element.disabled || element.readOnly) return false;
    if (element.type && /^(hidden|button|submit|reset|checkbox|radio)$/i.test(element.type)) return false;
    return isVisible(element);
  }
  function hasWritableEntryFields() {
    return Array.prototype.some.call(document.querySelectorAll("input,textarea,select"), isEditableField);
  }
  function isYundaPrefillReady() {
    if (document.readyState !== "complete") return false;
    if (!document.body) return false;
    var bodyText = clean(document.body.innerText || document.body.textContent || "");
    if (!bodyText || /AUTH_REQUIRED|AUTH_PENDING_CODE|登录态已失效|登录态已过期|验证码/.test(bodyText)) return false;
    return hasWritableEntryFields();
  }
  var prefillRunSerial = 0;
  var activePrefillKey = "";
  var activePrefillRunning = false;
  var prefillReadyTimer = 0;
  var prefillReadyNotified = false;
  function postPrefillReady() {
    try {
      window.parent.postMessage({
        type: "SHIPNOW_PREFILL_READY",
        provider: "yunda"
      }, shipnowParentOrigin);
    } catch (_) {}
  }
  function waitForYundaPrefillReady(attempt) {
    if (prefillReadyTimer) {
      window.clearTimeout(prefillReadyTimer);
      prefillReadyTimer = 0;
    }
    if (prefillReadyNotified) return;
    if (isYundaPrefillReady()) {
      prefillReadyNotified = true;
      postPrefillReady();
      return;
    }
    if (attempt < 80) {
      prefillReadyTimer = window.setTimeout(function () {
        waitForYundaPrefillReady(attempt + 1);
      }, 500);
    }
  }
  function notifyPrefillReadyWhenReady() {
    waitForYundaPrefillReady(0);
  }
  function runPrefill(message, attempt, serial) {
    var payload = message.payload || {};
    var specs = normalizeFields(payload);
    var filled = [];
    var missing = [];
    if (serial !== prefillRunSerial) return;
    if (!isYundaPrefillReady()) {
      if (attempt < 80) {
        window.setTimeout(function () { runPrefill(message, attempt + 1, serial); }, 500);
        return;
      }
      window.parent.postMessage({
        type: "SHIPNOW_PREFILL_RESULT",
        provider: "yunda",
        ok: false,
        filled: filled,
        missing: specs.map(function (spec) { return clean(spec && spec.key) || "unknown"; }),
        error: "韵达原页尚未加载完成"
      }, shipnowParentOrigin);
      if (serial === prefillRunSerial) activePrefillRunning = false;
      return;
    }
    specs.forEach(function (spec) {
      var result = fillSpec(spec);
      if (result.skipped) return;
      if (result.ok) filled.push(result.key);
      else missing.push(result.key);
    });
    if (missing.length && attempt < 80) {
      window.setTimeout(function () { runPrefill(message, attempt + 1, serial); }, 500);
      return;
    }
    window.parent.postMessage({
      type: "SHIPNOW_PREFILL_RESULT",
      provider: "yunda",
      ok: Boolean(filled.length),
      filled: filled,
      missing: missing
    }, shipnowParentOrigin);
    if (serial === prefillRunSerial) activePrefillRunning = false;
  }
  function startPrefill(message) {
    var payloadKey = clean(message && message.prefill_key);
    if (payloadKey && payloadKey === activePrefillKey && activePrefillRunning) {
      // Ignore repeated parent sends for the same payload; keep the current retry loop alive.
      return;
    }
    activePrefillKey = payloadKey || "";
    activePrefillRunning = true;
    prefillRunSerial += 1;
    runPrefill(message, 0, prefillRunSerial);
  }
  window.addEventListener("message", function (event) {
    var data = event.data || {};
    if (event.origin !== shipnowParentOrigin) return;
    if (data.type !== "SHIPNOW_PREFILL" || data.provider !== "yunda") return;
    startPrefill(data);
  });
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", notifyPrefillReadyWhenReady, { once: true });
  }
  window.addEventListener("load", notifyPrefillReadyWhenReady, { once: true });
  window.setTimeout(notifyPrefillReadyWhenReady, 300);
  window.setTimeout(notifyPrefillReadyWhenReady, 1200);
  notifyPrefillReadyWhenReady();
  window.codexManualPrefill.yunda = { setMiniValue: setMiniValue, run: startPrefill };
})();
</script>
"""
YUNDA_LOCAL_PRINT_HELPER = """
<script id="codex-yunda-local-print-script">
(function () {
  if (window.__shipnowYundaLocalPrintHook) return;
  window.__shipnowYundaLocalPrintHook = true;
  var shipnowParentOrigin = window.location.origin === "https://www.boyi.homes"
    ? "https://boyi.homes"
    : window.location.origin;
  var opened = {};
  function clean(value) { return String(value == null ? "" : value).trim(); }
  function parseJson(text) {
    try {
      var value = JSON.parse(clean(text));
      return value && typeof value === "object" ? value : null;
    } catch (_) {
      return null;
    }
  }
  function showPrintLink(url) {
    try {
      var existing = document.getElementById("codex-yunda-local-print-link");
      if (existing) existing.remove();
      var link = document.createElement("a");
      link.id = "codex-yunda-local-print-link";
      link.href = url;
      link.target = "_blank";
      link.rel = "noopener";
      link.textContent = "打开本地打印页";
      link.style.cssText = [
        "position:fixed",
        "right:24px",
        "bottom:24px",
        "z-index:2147483647",
        "padding:10px 14px",
        "border-radius:6px",
        "background:#111827",
        "color:#fff",
        "font-size:14px",
        "font-weight:700",
        "text-decoration:none",
        "box-shadow:0 12px 30px rgba(15,23,42,.28)"
      ].join(";");
      document.body.appendChild(link);
    } catch (_) {}
  }
  function openLocalPrint(payload) {
    var url = clean(payload && (payload.shipnow_autoprint_url || payload.shipnow_print_url));
    if (!url || opened[url]) return;
    opened[url] = true;
    try {
      window.parent.postMessage({
        type: "SHIPNOW_YUNDA_LOCAL_PRINT",
        url: url,
        preview_url: clean(payload.shipnow_print_url)
      }, shipnowParentOrigin);
    } catch (_) {}
    window.setTimeout(function () {
      var popup = null;
      try { popup = window.open(url, "_blank", "noopener"); } catch (_) {}
      if (!popup) showPrintLink(url);
    }, 0);
  }
  if (window.XMLHttpRequest && window.XMLHttpRequest.prototype) {
    var originalOpen = window.XMLHttpRequest.prototype.open;
    var originalSend = window.XMLHttpRequest.prototype.send;
    window.XMLHttpRequest.prototype.open = function (method, url) {
      this.__shipnowYundaRequestUrl = clean(url);
      return originalOpen.apply(this, arguments);
    };
    window.XMLHttpRequest.prototype.send = function () {
      this.addEventListener("readystatechange", function () {
        if (this.readyState !== 4) return;
        var data = parseJson(this.responseText);
        if (data && data.shipnow_autoprint_url) openLocalPrint(data);
      });
      return originalSend.apply(this, arguments);
    };
  }
  if (window.fetch) {
    var originalFetch = window.fetch;
    window.fetch = function () {
      return originalFetch.apply(this, arguments).then(function (response) {
        try {
          response.clone().text().then(function (text) {
            var data = parseJson(text);
            if (data && data.shipnow_autoprint_url) openLocalPrint(data);
          });
        } catch (_) {}
        return response;
      });
    };
  }
})();
</script>
"""


def _clean_text(value: Any) -> str:
    return str(value or "").strip()


def _is_allowed_path(path: str) -> bool:
    canonical = canonical_manual_proxy_path(path)
    return bool(canonical) and any(canonical.startswith(prefix) for prefix in ALLOWED_PATH_PREFIXES)


def _query_text(value: Any) -> str:
    if isinstance(value, dict):
        return urlencode([(str(key), str(item)) for key, item in value.items() if item is not None], doseq=True)
    return _clean_text(value).lstrip("?")


def _target_from_params(path_value: Any, query_value: Any = "") -> tuple[str, str, str]:
    raw_path = _clean_text(path_value) or urlparse(ENTRY_INDEX_URL).path
    raw_query = _query_text(query_value)
    parsed = urlparse(raw_path)

    if parsed.scheme or parsed.netloc:
        if parsed.scheme not in {"http", "https"} or parsed.netloc.lower() != urlparse(YUNDA_INMS_ORIGIN).netloc:
            raise ValueError("Only Yunda INMS URLs can be proxied.")
        path = canonical_manual_proxy_path(parsed.path)
        query_parts = parse_qsl(parsed.query, keep_blank_values=True)
    else:
        path = canonical_manual_proxy_path(
            raw_path if raw_path.startswith("/") else f"/{raw_path}"
        )
        query_parts = []

    if raw_query:
        query_parts.extend(parse_qsl(raw_query, keep_blank_values=True))
    if not _is_allowed_path(path):
        raise ValueError("Path is outside the Yunda public allow-list.")

    query = urlencode(query_parts, doseq=True)
    remote_url = urlunparse(("https", urlparse(YUNDA_INMS_ORIGIN).netloc, path, "", query, ""))
    return path, query, remote_url


def _filter_request_headers(headers: Any, *, content_type: str = "") -> dict[str, str]:
    output: dict[str, str] = {}
    if isinstance(headers, dict):
        for key, value in headers.items():
            key_text = _clean_text(key)
            if not key_text or key_text.lower() in HOP_BY_HOP_REQUEST_HEADERS:
                continue
            output[key_text] = str(value)
    if content_type and "content-type" not in {key.lower() for key in output}:
        output["Content-Type"] = content_type
    output["Origin"] = YUNDA_INMS_ORIGIN
    output["Referer"] = ENTRY_INDEX_URL
    return output


def _filter_response_headers(headers: Any) -> dict[str, str]:
    output: dict[str, str] = {}
    if isinstance(headers, dict):
        iterator = headers.items()
    else:
        iterator = getattr(headers, "items", lambda: [])()
    for key, value in iterator:
        key_text = _clean_text(key)
        if not key_text or key_text.lower() in BLOCKED_RESPONSE_HEADERS:
            continue
        output[key_text] = str(value)
    return output


def _decode_body(params: dict[str, Any]) -> bytes | None:
    raw_base64 = _clean_text(params.get("body_base64"))
    if raw_base64:
        return base64.b64decode(raw_base64)
    body_text = params.get("body")
    if body_text is None:
        return None
    return str(body_text).encode("utf-8")


def _charset_from_content_type(content_type: str) -> str:
    match = re.search(r"charset=([A-Za-z0-9._-]+)", content_type, flags=re.IGNORECASE)
    return match.group(1) if match else "utf-8"


def _proxy_url_for(remote_url: str, *, current_url: str, proxy_prefix: str) -> str:
    value = _clean_text(remote_url)
    if not value or value.startswith(("#", "$", "javascript:", "data:", "mailto:", "tel:")):
        return value
    absolute = urljoin(current_url, value)
    parsed = urlparse(absolute)
    if parsed.netloc and parsed.netloc.lower() != urlparse(YUNDA_INMS_ORIGIN).netloc:
        return value
    if not _is_allowed_path(parsed.path):
        return value
    proxied = f"{proxy_prefix.rstrip('/')}{parsed.path}"
    if parsed.query:
        proxied = f"{proxied}?{parsed.query}"
    if parsed.fragment:
        proxied = f"{proxied}#{parsed.fragment}"
    return proxied


def _rewrite_html_urls(html: str, *, current_url: str, proxy_prefix: str) -> str:
    def replace_attr(match: re.Match[str]) -> str:
        rewritten = _proxy_url_for(match.group("url"), current_url=current_url, proxy_prefix=proxy_prefix)
        return f"{match.group('prefix')}{match.group('quote')}{escape(rewritten, quote=True)}{match.group('quote')}"

    def replace_quoted(match: re.Match[str]) -> str:
        rewritten = _proxy_url_for(match.group("url"), current_url=current_url, proxy_prefix=proxy_prefix)
        return f"{match.group('quote')}{rewritten}{match.group('quote')}"

    def replace_unquoted_attr(match: re.Match[str]) -> str:
        rewritten = _proxy_url_for(match.group("url"), current_url=current_url, proxy_prefix=proxy_prefix)
        return f"{match.group('prefix')}{rewritten}"

    def replace_css_url(match: re.Match[str]) -> str:
        rewritten = _proxy_url_for(match.group("url"), current_url=current_url, proxy_prefix=proxy_prefix)
        return f"{match.group('prefix')}{match.group('quote')}{rewritten}{match.group('quote')}{match.group('suffix')}"

    rewritten = ATTR_URL_RE.sub(replace_attr, html)
    rewritten = UNQUOTED_ATTR_URL_RE.sub(replace_unquoted_attr, rewritten)
    rewritten = CSS_URL_RE.sub(replace_css_url, rewritten)
    return QUOTED_YUNDA_URL_RE.sub(replace_quoted, rewritten)


def _inject_cost_visibility_helper(html: str) -> str:
    helpers: list[str] = []
    if "codex-yunda-cost-style" not in html:
        helpers.append(YUNDA_COST_VISIBILITY_HELPER)
    if "codex-yunda-prefill-script" not in html:
        helpers.append(YUNDA_PREFILL_HELPER)
    if "codex-yunda-local-print-script" not in html:
        helpers.append(YUNDA_LOCAL_PRINT_HELPER)
    if not helpers:
        return html
    helper = "".join(helpers)
    if re.search(r"</body\s*>", html, flags=re.IGNORECASE):
        return re.sub(r"</body\s*>", lambda _: f"{helper}</body>", html, count=1, flags=re.IGNORECASE)
    return f"{html}{helper}"


def _should_rewrite_text_response(content_type: str) -> bool:
    lowered = content_type.lower()
    return any(
        marker in lowered
        for marker in (
            "text/html",
            "text/css",
            "text/javascript",
            "application/javascript",
            "application/x-javascript",
        )
    )


def _response_content(response: Any) -> bytes:
    content = getattr(response, "content", None)
    if isinstance(content, bytes):
        return content
    text = str(getattr(response, "text", "") or "")
    return text.encode("utf-8")


def run_once(params: dict[str, Any]) -> dict[str, Any]:
    params = params or {}
    method = _clean_text(params.get("method") or "GET").upper()
    if method not in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
        return {"ok": False, "error_code": "INVALID_PROXY_METHOD", "error": f"Unsupported method: {method}"}
    try:
        path, query, remote_url = _target_from_params(params.get("path"), params.get("query"))
    except ValueError as exc:
        return {"ok": False, "error_code": "INVALID_PROXY_PATH", "error": str(exc)}

    content_type = _clean_text(params.get("content_type"))
    headers = _filter_request_headers(params.get("headers"), content_type=content_type)
    body = _decode_body(params)
    session = get_session_broker("yunda").build_requests_session(validate=True)
    response = session.request(
        method,
        remote_url,
        headers=headers,
        data=body if method != "GET" else None,
        allow_redirects=True,
        timeout=int(params.get("timeout_sec") or DEFAULT_TIMEOUT_SEC),
    )
    raw_content = _response_content(response)
    _auth_if_login_response(response, raw_content.decode("utf-8", errors="replace"))

    response_headers = _filter_response_headers(getattr(response, "headers", {}))
    response_content_type = response_headers.get("content-type") or response_headers.get("Content-Type") or ""
    if _should_rewrite_text_response(response_content_type):
        charset = _charset_from_content_type(response_content_type)
        text = raw_content.decode(charset, errors="replace")
        rewritten = _rewrite_html_urls(
            text,
            current_url=str(getattr(response, "url", "") or remote_url),
            proxy_prefix=_clean_text(params.get("proxy_prefix")) or "/ocr/yunda/live",
        )
        if "text/html" in response_content_type.lower():
            rewritten = _inject_cost_visibility_helper(rewritten)
        raw_content = rewritten.encode(charset, errors="replace")

    return {
        "ok": True,
        "status_code": int(getattr(response, "status_code", 200) or 200),
        "headers": response_headers,
        "body_base64": base64.b64encode(raw_content).decode("ascii"),
        "remote_url": remote_url,
        "remote_path": path,
        "remote_query": query,
    }
