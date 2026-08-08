"""Ronghui TMS tracking adapter for the unified tracking query endpoint."""

from __future__ import annotations

import json
import re
import time
from typing import Any, Iterable
from urllib.parse import parse_qs, urlparse

from agent.tms_runtime.scripts import query_waybill_detail
from agent.tms_runtime.scripts.browser_manager import TMSBrowserAuth, launch_browser
from agent.tms_runtime.scripts.login_manager import TMSAuth


BASE_URL = "https://tms.ronghuiwl.com"
HOME_URL = f"{BASE_URL}/module/index?mv=index"
MENU_URL = f"{BASE_URL}/menuTreeExtend/loadMenu"
SCAN_ROWS_URL = f"{BASE_URL}/dataQuery/findAllByCallId?id=FIND_SACN_TRACK_BY_CODE"
SCAN_MAIN_ROWS_URL = f"{BASE_URL}/dataQuery/findAllByCallId?id=FIND_SACN_TRACK_BY_CODE_MAIN"
BILL_DETAIL_URL = f"{BASE_URL}/billEntity/getBillByCode"
TRACKING_MENU_TEXT = "快件跟踪"
DEFAULT_TIMEOUT_MS = 60_000
ARRIVAL_SCAN_TYPES = frozenset({"到件", "到达", "卸车"})

XPATH_BILL_TEXTAREA = (
    "//fieldset[@class=\"ux-fieldset\"]//span//textarea"
    "[@placeholder=\"多个单号请使用回车符分隔\"]"
)
XPATH_TRACK_IFRAME = "//iframe[@class=\"trackIframe\"]"
QUERY_BUTTON_XPATHS = (
    '//a[.//span[normalize-space(.)="查询"]]',
    '//a[normalize-space(.)="查询"]',
    '//span[normalize-space(.)="查询"]/ancestor::a[1]',
    '//a[.//span[normalize-space(.)="搜索"]]',
    '//a[normalize-space(.)="搜索"]',
)
SCAN_TAB_XPATHS = (
    "//span[normalize-space(.)='扫描记录']",
    "//td[normalize-space(.)='扫描记录']",
)
CHILD_TAB_XPATHS = (
    "//span[normalize-space(.)='子单分布']",
    "//td[normalize-space(.)='子单分布']",
)

_SPLIT_RE = re.compile(r"[,\s;]+")
_DECRYPT_DETAIL_FIELDS = (
    "sender_name",
    "sender_phone",
    "sender_address",
    "recipient_name",
    "recipient_phone",
    "recipient_address",
)
_DOWNLOAD_PATH_RE = re.compile(r"^(?:https?://[^/\s]+)?/?(?:unauth/download|file)/\S+$", re.IGNORECASE)
_DELIVERY_METHOD_ALIASES = {
    "doorstep delivery": "派送",
    "door delivery": "派送",
    "home delivery": "派送",
    "customer pickup": "自提",
    "self collection": "自提",
    "self-collection": "自提",
    "self pickup": "自提",
    "self-pickup": "自提",
    "pickup": "自提",
}


class RonghuiTmsTrackingError(RuntimeError):
    """Raised when Ronghui TMS tracking cannot be completed."""


JS_COLLECT_SCAN_RECORDS = r"""
() => {
  const fieldAliases = {
    scan_station: ["SCAN_SITE", "scanSite", "SCAN_SITE_NAME", "scanSiteName", "SITE_NAME", "siteName"],
    scan_time: ["SCAN_TIME", "scanTime", "SCAN_DATE", "scanDate", "CREATE_DATE", "createDate"],
    upload_time: ["UPLOAD_TIME", "uploadTime", "UP_TIME", "upTime"],
    transport_method: ["TRANSPORT_MODE", "transportMode", "TRANSPORT_TYPE", "transportType"],
    description: ["DESCRIPTION", "description", "SCAN_DESC", "scanDesc", "TRACK_DESC", "trackDesc"],
    contact: ["CONTACT", "contact", "PHONE", "phone", "TRACK_PHONE", "trackPhone"],
    remark: ["REMARK", "remark", "MEMO", "memo"],
    scan_type: ["SCAN_TYPE", "scanType", "TYPE_NAME", "typeName"],
    scan_user: ["SCAN_USER", "scanUser", "USER_NAME", "userName"],
    source: ["SOURCE", "source", "DATA_SOURCE", "dataSource"],
  };
  const labelAliases = {
    scan_station: ["扫描网点", "网点"],
    scan_time: ["扫描时间"],
    upload_time: ["上传时间"],
    transport_method: ["运输方式"],
    description: ["跟踪记录", "扫描轨迹", "描述"],
    contact: ["货物跟踪查询电话", "查询电话", "联系电话"],
    remark: ["备注"],
    scan_type: ["扫描类型", "类型"],
    scan_user: ["扫描人", "扫描员"],
    source: ["来源"],
  };

  function norm(v) {
    return v == null ? "" : String(v).replace(/\u00a0/g, " ").trim();
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
        try { if (frame.contentWindow) queue.push(frame.contentWindow); } catch (e) {}
      }
    }
    return out;
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
      const mapped = {};
      for (const [role, aliases] of Object.entries(fieldAliases)) {
        mapped[role] = roleColumns[role] ? norm(row[roleColumns[role].field]) : fieldValue(row, aliases);
      }
      if (mapped.scan_time || mapped.description || mapped.scan_station) out.push(mapped);
    }
    return out;
  }
  function headerIndexes(cells) {
    const normalized = cells.map(cell => labelText(cell));
    const result = {};
    for (const role of Object.keys(labelAliases)) result[role] = -1;
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
  function hasScanHeader(indexes) {
    return indexes.scan_time >= 0 && indexes.description >= 0;
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
        if (hasScanHeader(idx)) {
          header = idx;
          startIndex = i + 1;
          break;
        }
      }
      if (!header) continue;
      for (let i = startIndex; i < rows.length; i += 1) {
        const cells = Array.from(rows[i].querySelectorAll("th,td")).map(td => norm(td.innerText || td.textContent || ""));
        if (!cells.length) continue;
        const mapped = {};
        for (const role of Object.keys(labelAliases)) {
          mapped[role] = header[role] >= 0 ? norm(cells[header[role]]) : "";
        }
        if (mapped.scan_time || mapped.description || mapped.scan_station) out.push(mapped);
      }
    }
    return out;
  }

  const dedup = new Set();
  const rows = [];
  for (const w of collectWindows(window)) {
    try {
      if (w.mini && typeof w.mini.get === "function") {
        const ids = Array.from(w.document.querySelectorAll(".mini-datagrid")).map(el => el.id).filter(Boolean);
        for (const id of ids) {
          try {
            for (const row of extractGrid(w.mini.get(id))) {
              const key = Object.values(row).join("|");
              if (dedup.has(key)) continue;
              dedup.add(key);
              rows.push(row);
            }
          } catch (e) {}
        }
      }
    } catch (e) {}
    try {
      for (const row of extractTables(w.document)) {
        const key = Object.values(row).join("|");
        if (dedup.has(key)) continue;
        dedup.add(key);
        rows.push(row);
      }
    } catch (e) {}
  }
  return { rows };
}
"""


JS_COLLECT_CHILD_DISTRIBUTION = r"""
() => {
  const fieldAliases = {
    bill: ["BILL_CODE", "bill_code", "billCode", "BILL_NO", "billNo", "SUB_BILL", "subBill", "CHILD_BILL", "childBill"],
    site: ["SCAN_SITE", "scanSite", "SITE_NAME", "siteName", "SCAN_SITE_NAME", "scanSiteName"],
    type: ["SCAN_TYPE", "scanType", "TYPE_NAME", "typeName"],
    date: ["SCAN_DATE", "scanDate", "CREATE_DATE", "createDate", "SCAN_TIME", "scanTime"],
    desc: ["DESCRIPTION", "description", "SCAN_DESC", "scanDesc", "TRACK_DESC", "trackDesc"],
  };
  const labelAliases = {
    bill: ["子单号", "运单号", "单号"],
    site: ["扫描网点", "网点"],
    type: ["扫描类型", "类型"],
    date: ["扫描时间", "时间"],
    desc: ["扫描轨迹", "扫描记录", "描述", "说明", "备注"],
  };
  function norm(v) {
    return v == null ? "" : String(v).replace(/\u00a0/g, " ").trim();
  }
  function labelText(raw) {
    return norm(raw).replace(/\s+/g, "");
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
      let frames = [];
      try { frames = Array.from(w.document.querySelectorAll("iframe")); } catch (e) {}
      for (const frame of frames) {
        try { if (frame.contentWindow) queue.push(frame.contentWindow); } catch (e) {}
      }
    }
    return out;
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
      if (bill) out.push({ bill, site, type, date, desc });
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
      const trs = Array.from(table.querySelectorAll("tr"));
      let header = null;
      let startIndex = 0;
      for (let i = 0; i < trs.length; i += 1) {
        const cells = Array.from(trs[i].querySelectorAll("th,td")).map(td => norm(td.innerText || td.textContent || ""));
        if (!cells.length) continue;
        const idx = headerIndexes(cells);
        if (idx.bill >= 0 && idx.site >= 0) {
          header = idx;
          startIndex = i + 1;
          break;
        }
      }
      if (!header) continue;
      for (let i = startIndex; i < trs.length; i += 1) {
        const cells = Array.from(trs[i].querySelectorAll("th,td")).map(td => norm(td.innerText || td.textContent || ""));
        const bill = header.bill >= 0 ? norm(cells[header.bill]) : "";
        if (!bill) continue;
        out.push({
          bill,
          site: header.site >= 0 ? norm(cells[header.site]) : "",
          type: header.type >= 0 ? norm(cells[header.type]) : "",
          date: header.date >= 0 ? norm(cells[header.date]) : "",
          desc: header.desc >= 0 ? norm(cells[header.desc]) : "",
        });
      }
    }
    return out;
  }

  const rows = [];
  const dedup = new Set();
  for (const w of collectWindows(window)) {
    try {
      if (w.mini && typeof w.mini.get === "function") {
        const ids = Array.from(w.document.querySelectorAll(".mini-datagrid")).map(el => el.id).filter(Boolean);
        for (const id of ids) {
          try {
            for (const row of extractGrid(w.mini.get(id))) {
              const key = [row.bill, row.site, row.type, row.date, row.desc].join("|");
              if (dedup.has(key)) continue;
              dedup.add(key);
              rows.push(row);
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
      }
    } catch (e) {}
  }
  return { rows };
}
"""


def _clean_str(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _normalize_bill_code(value: Any) -> str:
    text = _clean_str(value)
    if text.startswith("="):
        text = text[1:].strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {"'", '"'}:
        text = text[1:-1].strip()
    return re.sub(r"\s+", "", text)


def _resolve_tracking_number(params: dict[str, Any]) -> str:
    for key in (
        "tracking_number",
        "trackingNumber",
        "bill_code",
        "billCode",
        "waybill_no",
        "waybillNo",
        "ship_id",
        "shipId",
    ):
        code = _normalize_bill_code(params.get(key))
        if code:
            return code
    raw_items = params.get("items")
    if isinstance(raw_items, list):
        for item in raw_items:
            code = _resolve_tracking_number(item) if isinstance(item, dict) else _normalize_bill_code(item)
            if code:
                return code
    return ""


def _coerce_bool(value: Any, *, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def _coerce_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _value_with_unit(value: Any, unit: str) -> str:
    text = _clean_str(value)
    if not text:
        return ""
    return text if text.endswith(unit) else f"{text}{unit}"


def _first_value(row: dict[str, Any], keys: tuple[str, ...]) -> str:
    for key in keys:
        value = _clean_str(row.get(key))
        if value:
            return value
    return ""


def _route_text_value(value: Any) -> str:
    text = _clean_str(value)
    if _DOWNLOAD_PATH_RE.match(text):
        return ""
    return text


def _first_route_text(row: dict[str, Any], keys: tuple[str, ...]) -> str:
    for key in keys:
        value = _route_text_value(row.get(key))
        if value:
            return value
    return ""


def _normalize_delivery_method(value: Any) -> str:
    text = _clean_str(value)
    if not text:
        return ""
    compact = re.sub(r"\s+", " ", text).strip().lower()
    return _DELIVERY_METHOD_ALIASES.get(compact, text)


def _is_masked_value(value: Any) -> bool:
    text = _clean_str(value)
    return "*" in text or "＊" in text


def _needs_decrypted_detail(row: dict[str, Any] | None) -> bool:
    if not row:
        return True
    for key in ("sender_name", "sender_phone", "recipient_name", "recipient_phone"):
        value = row.get(key)
        if not _clean_str(value) or _is_masked_value(value):
            return True
    return False


def _matching_detail_overlay(rows: list[dict[str, Any]], bill_code: str) -> dict[str, Any] | None:
    normalized_code = _normalize_bill_code(bill_code)
    matches: list[dict[str, Any]] = []
    for row in rows:
        candidates = (
            row.get("tracking_number"),
            row.get("requested_bill_code"),
            row.get("bill_code"),
            row.get("billCode"),
            row.get("waybill_no"),
        )
        if any(_normalize_bill_code(candidate) == normalized_code for candidate in candidates):
            matches.append(row)
    if len(matches) == 1:
        return matches[0]
    if not matches and len(rows) == 1:
        return rows[0]
    return None


def _prefer_decrypted_value(current: Any, overlay: Any, *, address: bool = False) -> str:
    current_text = _clean_str(current)
    overlay_text = _clean_str(overlay)
    if not overlay_text:
        return current_text
    if not current_text:
        return overlay_text
    if _is_masked_value(current_text) and not _is_masked_value(overlay_text):
        return overlay_text
    if address and not _is_masked_value(overlay_text) and len(overlay_text) > len(current_text):
        return overlay_text
    return current_text


def _merge_decrypted_detail(
    detail_row: dict[str, Any] | None,
    overlay_row: dict[str, Any] | None,
    bill_code: str,
) -> dict[str, Any]:
    merged = dict(detail_row or {})
    if not merged.get("tracking_number"):
        merged["tracking_number"] = bill_code
    if not overlay_row:
        return merged
    for key in _DECRYPT_DETAIL_FIELDS:
        merged[key] = _prefer_decrypted_value(
            merged.get(key),
            overlay_row.get(key),
            address=key.endswith("_address"),
        )
    for key, value in overlay_row.items():
        if key in _DECRYPT_DETAIL_FIELDS or value in (None, ""):
            continue
        if merged.get(key) in (None, ""):
            merged[key] = value
    return merged


def _overlay_decrypted_detail(
    *,
    params: dict[str, Any],
    bill_code: str,
    detail_row: dict[str, Any] | None,
) -> dict[str, Any]:
    if not _coerce_bool(params.get("decrypt_masked"), default=True):
        return detail_row or {}
    if not _needs_decrypted_detail(detail_row):
        return detail_row or {}

    rows = query_waybill_detail.query_waybill_details(
        bill_codes=[bill_code],
        max_workers=1,
        decrypt_masked=True,
        browser_headless=_coerce_bool(params.get("browser_headless"), default=True),
        browser_timeout_ms=_coerce_int(params.get("browser_timeout_ms"), 30_000),
        browser_batch_size=1,
        browser_max_workers=1,
    )
    overlay_row = _matching_detail_overlay([row for row in rows if isinstance(row, dict)], bill_code)
    if not overlay_row:
        raise RonghuiTmsTrackingError(f"融辉详情解密未返回匹配单号：{bill_code}")

    merged = _merge_decrypted_detail(detail_row, overlay_row, bill_code)
    if _needs_decrypted_detail(merged):
        raise RonghuiTmsTrackingError(f"融辉详情仍为脱敏字段，未拿到解密客户信息：{bill_code}")
    return merged


def _api_track_description(row: dict[str, Any]) -> str:
    explicit = _first_route_text(row, ("description", "desc", "DES", "TRACK_DESC", "SCAN_DESC"))
    if explicit:
        return explicit
    station = _first_value(row, ("SCAN_SITE", "scan_station", "site"))
    scan_type = _first_value(row, ("SCAN_TYPE", "scan_type", "type"))
    scan_user = _first_value(row, ("SCAN_MAN", "scan_user", "user"))
    related_station = _first_value(row, ("PRE_OR_NEXT_STATION", "pre_or_next_station", "next_station"))
    if not station and not scan_type:
        return ""

    if scan_type in {"发件", "发件扫描", "装车"}:
        action = "已装车" if scan_type in {"发件", "发件扫描"} else "完成装车"
        text = f"快件在【{station}】{action}"
        if related_station:
            text += f",正发往【{related_station}】"
    elif scan_type in {"到件", "到达", "卸车"}:
        action = "到达" if scan_type in {"到件", "到达"} else "完成卸车"
        text = f"快件{action}【{station}】" if scan_type in {"到件", "到达"} else f"快件在【{station}】{action}"
        if related_station:
            text += f",上一站是【{related_station}】"
    elif scan_type in {"收件", "收件扫描", "寄件"}:
        text = f"快件在【{station}】完成收件扫描"
    elif scan_type in {"派件", "派件扫描"}:
        text = f"快件在【{station}】进行派件扫描"
    elif scan_type in {"签收", "签收扫描"}:
        text = f"快件在【{station}】完成签收扫描"
    elif scan_type:
        text = f"快件在【{station}】已进行{scan_type}扫描" if station else f"快件已进行{scan_type}扫描"
    else:
        text = f"快件在【{station}】有扫描记录"

    if scan_user:
        text += f",扫描员是【{scan_user}】"
    return f"{text};"


def _api_contact_text(row: dict[str, Any]) -> str:
    parts: list[str] = []
    station = _first_value(row, ("SCAN_SITE", "scan_station", "site"))
    phone = _first_value(row, ("SCAN_SITE_PHONE", "scan_site_phone", "contact", "phone"))
    if station and phone:
        parts.append(f"{station}:{phone}")
    elif phone:
        parts.append(phone)
    related_station = _first_value(row, ("PRE_OR_NEXT_STATION", "pre_or_next_station", "next_station"))
    related_phone = _first_value(row, ("PRE_OR_NEXT_STATION_PHONE", "pre_or_next_station_phone"))
    if related_station and related_phone:
        parts.append(f"{related_station}:{related_phone}")
    elif related_phone:
        parts.append(related_phone)
    return "\n".join(dict.fromkeys(parts))


def _map_api_route_row(row: dict[str, Any]) -> dict[str, str]:
    return {
        "scan_station": _first_value(row, ("SCAN_SITE", "scan_station", "site", "station")),
        "scan_time": _first_value(row, ("SCAN_DATE", "SCAN_TIME", "scan_time", "date", "time")),
        "upload_time": _first_value(row, ("REGISTER_DATE", "UPLOAD_TIME", "upload_time")),
        "transport_method": _first_value(row, ("CLASS", "TRANSPORT_MODE", "transport_method", "transport")),
        "description": _api_track_description(row),
        "contact": _api_contact_text(row),
        "remark": _first_value(row, ("REMARK", "remark", "MEMO")),
        "scan_type": _first_value(row, ("SCAN_TYPE", "scan_type", "type")),
        "scan_user": _first_value(row, ("SCAN_MAN", "scan_user", "user")),
        "source": _first_value(row, ("DATA_FROM", "DATA_SOURCE", "source", "data_source")),
    }


def _map_api_child_row(row: dict[str, Any]) -> dict[str, str]:
    mapped = _map_api_route_row(row)
    return {
        "child_waybill_no": _normalize_bill_code(
            row.get("BILL_CODE") or row.get("child_waybill_no") or row.get("bill") or row.get("bill_code")
        ),
        "scan_station": mapped["scan_station"],
        "scan_type": mapped["scan_type"],
        "scan_time": mapped["scan_time"],
        "description": mapped["description"],
    }


def _normalize_api_detail_row(row: dict[str, Any], tracking_number: str) -> dict[str, Any]:
    return {
        "tracking_number": _first_value(row, ("BILL_CODE", "bill_code", "tracking_number")) or tracking_number,
        "sender_site": _first_value(row, ("SEND_SITE", "send_site", "REGISTER_SITE")),
        "destination_station": _first_value(row, ("DESTINATION", "DISPATCH_SITE", "SIGN_SITE", "disp_site")),
        "delivery_method": _normalize_delivery_method(
            _first_value(row, ("DISPATCH_MODE_TEXT", "DISPATCH_MODE", "delivery_method"))
        ),
        "payment_type": _first_value(row, ("PAYMENT_TYPE", "PAYMENT_SIDE", "payment_type")),
        "insurance_amount": _first_value(row, ("INSURANCE", "INSURE_MONEY", "REAL_VALUE", "insurance_amount")),
        "pay_on_arrival": _first_value(row, ("GOODS_PAYMENT", "AGENCY_FUND", "cod_amount", "pay_on_arrival")),
        "remark": _first_value(row, ("REMARK", "INSURE_REMARK", "remark")),
        "sender_name": _first_value(row, ("SEND_MAN", "sender_name")),
        "sender_phone": _first_value(row, ("SEND_MAN_PHONE", "SEND_MAN_TEL", "sender_phone")),
        "recipient_name": _first_value(row, ("ACCEPT_MAN", "recipient_name")),
        "recipient_phone": _first_value(row, ("ACCEPT_MAN_PHONE", "ACCEPT_MAN_TEL", "recipient_phone")),
        "sender_address": _first_value(row, ("SEND_MAN_ADDRESS", "sender_address")),
        "recipient_address": _first_value(row, ("ACCEPT_MAN_ADDRESS", "recipient_address")),
        "goods_name": _first_value(row, ("GOODS_NAME", "goods_name")),
        "package_type": _first_value(row, ("PACK_TYPE", "PACKAGING", "package_type")),
        "quantity": _first_value(row, ("GOODS_COUNT", "PCS", "PIECE", "quantity")),
        "actual_weight": _first_value(row, ("SETTLEMENT_WEIGHT", "BILL_WEIGHT", "WEIGHT", "actual_weight")),
        "volume": _first_value(row, ("VOLUME", "volume")),
        "volumetric_weight": _first_value(row, ("VOLUME_WEIGHT", "volume_weight", "volumetric_weight")),
        "shipping_fee": _first_value(row, ("FREIGHT", "CUSTOMER_FREIGHT", "GUEST_FREIGHT", "shipping_fee")),
    }


def normalize_route_row(row: dict[str, Any]) -> dict[str, str]:
    if any(key in row for key in ("SCAN_SITE", "SCAN_DATE", "SCAN_TYPE", "REGISTER_DATE", "DATA_FROM")):
        return _map_api_route_row(row)
    return {
        "scan_station": _clean_str(row.get("scan_station") or row.get("site") or row.get("station")),
        "scan_time": _clean_str(row.get("scan_time") or row.get("date") or row.get("time")),
        "upload_time": _clean_str(row.get("upload_time")),
        "transport_method": _clean_str(row.get("transport_method") or row.get("transport")),
        "description": _first_route_text(row, ("description", "desc", "record")),
        "contact": _clean_str(row.get("contact") or row.get("phone")),
        "remark": _clean_str(row.get("remark")),
        "scan_type": _clean_str(row.get("scan_type") or row.get("type")),
        "scan_user": _clean_str(row.get("scan_user") or row.get("user")),
        "source": _clean_str(row.get("source") or row.get("data_source")),
    }


def normalize_child_detail_row(row: dict[str, Any]) -> dict[str, str]:
    if any(key in row for key in ("BILL_CODE", "SCAN_SITE", "SCAN_DATE", "SCAN_TYPE")):
        return _map_api_child_row(row)
    return {
        "child_waybill_no": _normalize_bill_code(
            row.get("child_waybill_no")
            or row.get("bill")
            or row.get("bill_code")
            or row.get("waybill_no")
            or row.get("tracking_number")
        ),
        "scan_station": _clean_str(row.get("scan_station") or row.get("site")),
        "scan_type": _clean_str(row.get("scan_type") or row.get("type")),
        "scan_time": _clean_str(row.get("scan_time") or row.get("date") or row.get("time")),
        "description": _first_route_text(row, ("description", "desc", "record")),
    }


def _normalize_route_rows(rows: Iterable[Any]) -> list[dict[str, str]]:
    normalized: list[dict[str, str]] = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        mapped = normalize_route_row(row)
        if mapped["scan_time"] or mapped["description"] or mapped["scan_station"]:
            normalized.append(mapped)
    return normalized


def _normalize_child_rows(rows: Iterable[Any]) -> list[dict[str, str]]:
    normalized: list[dict[str, str]] = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        mapped = normalize_child_detail_row(row)
        if mapped["child_waybill_no"]:
            normalized.append(mapped)
    return normalized


def _arrival_progress_from_child_distribution(
    *,
    tracking_number: str,
    routes: list[dict[str, str]],
    children: list[dict[str, str]],
    detail: dict[str, Any],
) -> dict[str, Any]:
    """Build exact live arrival progress from the latest TMS child distribution."""
    if not routes or not children:
        return {}

    main_bill_code = _normalize_bill_code(tracking_number)
    route_times = [_clean_str(route.get("scan_time")) for route in routes]
    if any(not route_time for route_time in route_times):
        return {}
    latest_route_time = max(route_times)
    latest_routes = [route for route in routes if _clean_str(route.get("scan_time")) == latest_route_time]
    latest_contexts = {
        (_clean_str(route.get("scan_station")), _clean_str(route.get("scan_type")))
        for route in latest_routes
    }
    if len(latest_contexts) != 1:
        return {}
    latest_route = latest_routes[0]
    arrival_station = _clean_str(latest_route.get("scan_station"))
    if (
        not main_bill_code
        or not arrival_station
        or _clean_str(latest_route.get("scan_type")) not in ARRIVAL_SCAN_TYPES
    ):
        return {}

    latest_children: dict[str, dict[str, str]] = {}
    for child in children:
        child_bill_code = _normalize_bill_code(child.get("child_waybill_no"))
        child_suffix = child_bill_code[len(main_bill_code) :] if child_bill_code.startswith(main_bill_code) else ""
        if (
            not child_bill_code
            or child_bill_code == main_bill_code
            or not re.fullmatch(r"\d{4}", child_suffix)
        ):
            continue
        child_time = _clean_str(child.get("scan_time"))
        child_station = _clean_str(child.get("scan_station"))
        child_type = _clean_str(child.get("scan_type"))
        if not child_time or not child_station or not child_type:
            return {}
        previous = latest_children.get(child_bill_code)
        if previous is None or child_time > _clean_str(previous.get("scan_time")):
            latest_children[child_bill_code] = child
            continue
        if child_time == _clean_str(previous.get("scan_time")) and (
            child_station != _clean_str(previous.get("scan_station"))
            or child_type != _clean_str(previous.get("scan_type"))
        ):
            return {}

    if not latest_children:
        return {}

    arrived_children = {
        child_bill_code: child
        for child_bill_code, child in latest_children.items()
        if _clean_str(child.get("scan_station")) == arrival_station
        and _clean_str(child.get("scan_type")) in ARRIVAL_SCAN_TYPES
    }

    progress: dict[str, Any] = {
        "arrived_quantity": len(arrived_children),
        "arrival_station": arrival_station,
        "source": "ronghui_tms_child_distribution",
    }
    arrival_times = sorted(
        time_text
        for time_text in (_clean_str(row.get("scan_time")) for row in arrived_children.values())
        if time_text
    )
    if arrival_times:
        progress["first_arrival_at"] = arrival_times[0]
        progress["last_arrival_at"] = arrival_times[-1]

    expected_text = _clean_str(detail.get("quantity"))
    if re.fullmatch(r"\d+", expected_text):
        expected_quantity = int(expected_text)
        progress["expected_quantity"] = expected_quantity
        if expected_quantity >= progress["arrived_quantity"]:
            progress["pending_quantity"] = expected_quantity - progress["arrived_quantity"]
            if progress["arrived_quantity"] == 0:
                progress["arrival_status"] = "pending"
            elif expected_quantity == progress["arrived_quantity"]:
                progress["arrival_status"] = "completed"
            else:
                progress["arrival_status"] = "partial"
    return progress


def _kv(label: str, value: Any, *, unit: str = "") -> dict[str, str] | None:
    text = _value_with_unit(value, unit) if unit else _clean_str(value)
    if not text:
        return None
    return {"label": label, "value": text}


def _compact_items(items: list[dict[str, str] | None]) -> list[dict[str, str]]:
    return [item for item in items if item]


def _build_waybill_stub(detail: dict[str, Any], tracking_number: str) -> dict[str, str]:
    if not detail:
        return {"waybill_no": tracking_number}
    quantity = detail.get("quantity")
    return {
        "waybill_no": _clean_str(detail.get("tracking_number")) or tracking_number,
        "send_site": _clean_str(detail.get("sender_site") or detail.get("send_site")),
        "disp_site": _clean_str(detail.get("destination_station") or detail.get("destination_site")),
        "delivery_method": _clean_str(detail.get("delivery_method")),
        "payment_type": _clean_str(detail.get("payment_type")),
        "insurance_amount": _clean_str(detail.get("insurance_amount")),
        "cod_amount": _clean_str(detail.get("pay_on_arrival") or detail.get("cod_amount")),
        "remark": _clean_str(detail.get("remarks") or detail.get("remark")),
        "sender_name": _clean_str(detail.get("sender_name")),
        "sender_phone": _clean_str(detail.get("sender_phone")),
        "recipient_name": _clean_str(detail.get("recipient_name")),
        "recipient_phone": _clean_str(detail.get("recipient_phone")),
        "send_address": _clean_str(detail.get("sender_address")),
        "recipient_address": _clean_str(detail.get("recipient_address")),
        "disp_address": _clean_str(detail.get("recipient_address")),
        "goods_name": _clean_str(detail.get("goods_name")),
        "package_type": _clean_str(detail.get("package_type")),
        "weight": _value_with_unit(detail.get("actual_weight"), "kg"),
        "volume": _value_with_unit(detail.get("volume"), "方"),
        "volume_weight": _clean_str(detail.get("volumetric_weight")),
        "pieces": _value_with_unit(quantity, "件") if quantity not in (None, "") else "",
        "shipping_fee": _clean_str(detail.get("shipping_fee")),
    }


def _build_waybill_info(detail: dict[str, Any], tracking_number: str) -> list[dict[str, Any]]:
    if not detail:
        return []
    stub = _build_waybill_stub(detail, tracking_number)
    return [
        {
            "title": "基础信息",
            "items": _compact_items(
                [
                    _kv("运单号", stub.get("waybill_no")),
                    _kv("送货方式", stub.get("delivery_method")),
                    _kv("目的网点", stub.get("disp_site")),
                    _kv("备注", stub.get("remark")),
                ]
            ),
        },
        {
            "title": "发货信息",
            "items": _compact_items(
                [
                    _kv("发货人", stub.get("sender_name")),
                    _kv("发货电话", stub.get("sender_phone")),
                    _kv("发货网点", stub.get("send_site")),
                    _kv("发货地址", stub.get("send_address")),
                ]
            ),
        },
        {
            "title": "收货信息",
            "items": _compact_items(
                [
                    _kv("收货人", stub.get("recipient_name")),
                    _kv("收货电话", stub.get("recipient_phone")),
                    _kv("目的网点", stub.get("disp_site")),
                    _kv("收件地址", stub.get("recipient_address") or stub.get("disp_address")),
                ]
            ),
        },
        {
            "title": "货物信息",
            "items": _compact_items(
                [
                    _kv("货物名称", stub.get("goods_name")),
                    _kv("包装类型", stub.get("package_type")),
                    _kv("件数", stub.get("pieces")),
                    _kv("实际重量", stub.get("weight")),
                    _kv("体积", stub.get("volume")),
                    _kv("体积重量", stub.get("volume_weight")),
                ]
            ),
        },
        {
            "title": "费用信息",
            "items": _compact_items(
                [
                    _kv("付款方式", stub.get("payment_type")),
                    _kv("运费", stub.get("shipping_fee")),
                    _kv("保价金额", stub.get("insurance_amount")),
                    _kv("代收货款", stub.get("cod_amount")),
                ]
            ),
        },
    ]


def build_tracking_result(
    *,
    tracking_number: str,
    route_rows: list[dict[str, Any]],
    detail_row: dict[str, Any] | None,
    child_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    routes = _normalize_route_rows(route_rows)
    children = _normalize_child_rows(child_rows)
    detail = detail_row if isinstance(detail_row, dict) else {}
    info = [section for section in _build_waybill_info(detail, tracking_number) if section.get("items")]
    latest = routes[-1] if routes else {}
    result: dict[str, Any] = {
        "ok": True,
        "type": "ronghui_tms",
        "tracking_number": tracking_number,
        "requested_tracking_number": tracking_number,
        "summary": {
            "route_count": len(routes),
            "latest_time": latest.get("scan_time", ""),
            "latest_description": latest.get("description", ""),
            "status": latest.get("scan_type", ""),
        },
        "route_rows": routes,
        "child_detail_rows": children,
        "counts": {
            "route_rows": len(routes),
            "child_detail_rows": len(children),
        },
    }
    arrival_progress = _arrival_progress_from_child_distribution(
        tracking_number=tracking_number,
        routes=routes,
        children=children,
        detail=detail,
    )
    if arrival_progress:
        result["arrival_progress"] = arrival_progress
    stub = _build_waybill_stub(detail, tracking_number)
    if any(value for key, value in stub.items() if key != "waybill_no"):
        result["waybill_stub"] = stub
    if info:
        result["waybill_info"] = info
        result["counts"]["waybill_info_sections"] = len(info)
    return result


def _safe_json(resp: Any) -> Any:
    try:
        return resp.json()
    except Exception:
        return None


def _rows_from_tms_payload(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if not isinstance(payload, dict):
        return []
    candidates = [payload.get("data"), payload.get("rows")]
    result = payload.get("result")
    if isinstance(result, dict):
        candidates.extend((result.get("data"), result.get("rows")))
    elif isinstance(result, list):
        candidates.append(result)
    for candidate in candidates:
        if isinstance(candidate, list):
            return [row for row in candidate if isinstance(row, dict)]
    return []


def _tracking_page_headers(tracking_url: str) -> dict[str, str]:
    headers = {
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "X-Requested-With": "XMLHttpRequest",
        "Referer": tracking_url or HOME_URL,
    }
    query = parse_qs(urlparse(tracking_url or "").query, keep_blank_values=True)
    authentication_key = _clean_str((query.get("authenticationKey") or [""])[0])
    page_id = _clean_str((query.get("pageId") or [""])[0])
    if authentication_key:
        headers["authenticationKey"] = authentication_key
    if page_id:
        headers["pageId"] = page_id
    return headers


def _post_form_rows(
    session: Any,
    url: str,
    form: dict[str, str],
    *,
    tracking_url: str = "",
) -> list[dict[str, Any]]:
    response = session.post(
        url,
        data=form,
        headers=_tracking_page_headers(tracking_url),
        timeout=30,
    )
    response.raise_for_status()
    return _rows_from_tms_payload(_safe_json(response))


def _latest_child_scan_rows(rows: list[dict[str, Any]], main_bill_code: str) -> list[dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    for row in rows:
        bill_code = _normalize_bill_code(row.get("BILL_CODE"))
        if not bill_code or bill_code == main_bill_code:
            continue
        current_time = _first_value(row, ("SCAN_DATE", "SCAN_TIME", "scan_time"))
        previous = latest.get(bill_code)
        previous_time = _first_value(previous or {}, ("SCAN_DATE", "SCAN_TIME", "scan_time"))
        if previous is None or current_time > previous_time:
            latest[bill_code] = row
    return [latest[key] for key in sorted(latest)]


def _collect_api_tracking_rows(
    session: Any,
    bill_code: str,
    *,
    tracking_url: str = "",
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    all_scan_rows = _post_form_rows(session, SCAN_ROWS_URL, {"BILL_CODE": bill_code}, tracking_url=tracking_url)
    route_rows = _post_form_rows(session, SCAN_MAIN_ROWS_URL, {"BILL_CODE": bill_code}, tracking_url=tracking_url)
    if not route_rows:
        route_rows = [
            row
            for row in all_scan_rows
            if _normalize_bill_code(row.get("BILL_CODE")) == bill_code
        ]
    detail_rows = _post_form_rows(session, BILL_DETAIL_URL, {"billCode": bill_code}, tracking_url=tracking_url)
    detail = {}
    for row in detail_rows:
        if _normalize_bill_code(row.get("BILL_CODE")) == bill_code:
            detail = _normalize_api_detail_row(row, bill_code)
            break
    return route_rows, _latest_child_scan_rows(all_scan_rows, bill_code), detail


def _menu_label(node: dict[str, Any]) -> str:
    for key in ("text", "name", "menuName", "title", "TEXT", "NAME", "MENU_NAME", "TITLE"):
        text = _clean_str(node.get(key))
        if text:
            return text
    return ""


def _menu_url(node: dict[str, Any]) -> str:
    for key in ("url", "href", "link", "URL", "HREF", "LINK"):
        url = _clean_str(node.get(key))
        if url:
            return url
    return ""


def _menu_children(node: dict[str, Any]) -> list[Any]:
    for key in ("children", "data", "nodes", "items", "CHILDREN", "DATA", "NODES", "ITEMS"):
        value = node.get(key)
        if isinstance(value, list):
            return value
    return []


def _walk_menu_nodes(nodes: Iterable[Any], path: str = ""):
    for node in nodes or []:
        if not isinstance(node, dict):
            continue
        text = _menu_label(node)
        current_path = f"{path}/{text}" if path else text
        yield node, current_path
        yield from _walk_menu_nodes(_menu_children(node), current_path)


def _resolve_widget_menu_url(session: Any, leaf_text: str = TRACKING_MENU_TEXT) -> str:
    response = session.get(MENU_URL, timeout=20)
    response.raise_for_status()
    payload = _safe_json(response) or {}
    result = payload.get("result")
    if isinstance(result, dict):
        menu_data = result.get("data") if isinstance(result.get("data"), list) else [result]
    elif isinstance(result, list):
        menu_data = result
    elif isinstance(payload, list):
        menu_data = payload
    else:
        menu_data = [payload]
    for node, _path in _walk_menu_nodes(menu_data or []):
        text = _menu_label(node)
        url = _menu_url(node)
        if text != leaf_text or not url or "/widget/home?" not in url:
            continue
        return url if url.startswith("http") else f"{BASE_URL}{url}"
    raise RonghuiTmsTrackingError(f"未找到融辉 TMS 菜单入口：{leaf_text}")


def _wait_network_idle(page: Any, timeout_ms: int = 10_000) -> None:
    try:
        page.wait_for_load_state("networkidle", timeout=timeout_ms)
    except Exception:
        pass


def _goto(page: Any, url: str, *, timeout_ms: int = 60_000) -> None:
    page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
    _wait_network_idle(page)


def _click_first_xpath(scope: Any, xpaths: tuple[str, ...], *, timeout_ms: int) -> None:
    last_error: BaseException | None = None
    for xpath in xpaths:
        try:
            locator = scope.locator(f"xpath={xpath}").first
            locator.wait_for(state="visible", timeout=timeout_ms)
            locator.scroll_into_view_if_needed()
            locator.click()
            return
        except Exception as exc:
            last_error = exc
    raise TimeoutError(f"未找到可点击元素：{xpaths[0]}") from last_error


def _fill_tracking_bill_input(scope: Any, bill_code: str, *, timeout_ms: int) -> None:
    try:
        done = scope.evaluate(
            """
            (bill) => {
              const miniObj = window.mini && window.mini.get ? window.mini.get("BILL_CODE") : null;
              if (!miniObj) return false;
              if (typeof miniObj.setValue === "function") miniObj.setValue(bill);
              if (typeof miniObj.setText === "function") miniObj.setText(bill);
              if (typeof miniObj.doValueChanged === "function") miniObj.doValueChanged();
              return true;
            }
            """,
            bill_code,
        )
        if done:
            return
    except Exception:
        pass
    locator = scope.locator(f"xpath={XPATH_BILL_TEXTAREA}").first
    locator.wait_for(state="visible", timeout=timeout_ms)
    locator.fill(bill_code)


def _click_query(scope: Any, *, timeout_ms: int) -> None:
    try:
        clicked = scope.evaluate(
            """
            () => {
              const btn = window.mini && window.mini.get ? window.mini.get("searchBtn") : null;
              if (!btn) return false;
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
    _click_first_xpath(scope, QUERY_BUTTON_XPATHS, timeout_ms=timeout_ms)


def _wait_track_frame(scope: Any, *, timeout_ms: int):
    deadline = time.time() + timeout_ms / 1000.0
    last_error: BaseException | None = None
    while time.time() < deadline:
        try:
            iframe = scope.locator(f"xpath={XPATH_TRACK_IFRAME}").first
            if iframe.count() == 0:
                scope.wait_for_timeout(200)
                continue
            handle = iframe.element_handle()
            frame = handle.content_frame() if handle is not None else None
            if frame is None or not frame.url or frame.url == "about:blank":
                scope.wait_for_timeout(200)
                continue
            try:
                text = frame.locator("body").inner_text(timeout=1_000)
            except Exception:
                text = ""
            if text.strip():
                return frame
        except Exception as exc:
            last_error = exc
        scope.wait_for_timeout(200)
    raise TimeoutError("未找到融辉 TMS trackIframe") from last_error


def _wait_bill_loaded(frame: Any, bill_code: str, *, timeout_ms: int) -> None:
    frame.wait_for_function(
        """(bill) => (document.body ? document.body.innerText || "" : "").includes(bill)""",
        arg=bill_code,
        timeout=timeout_ms,
    )


def _collect_page_tracking_rows(
    bill_code: str,
    *,
    session_profile: str,
    headless: bool,
    slow_mo_ms: int,
    timeout_ms: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    session = TMSAuth(profile=session_profile).login_and_get_session()
    tracking_url = _resolve_widget_menu_url(session)
    p = browser = context = page = None
    try:
        p, browser, context, page = launch_browser(
            headless=headless,
            slow_mo_ms=slow_mo_ms,
            profile=session_profile,
        )
        auth = TMSBrowserAuth(home_url=HOME_URL, profile=session_profile)
        auth.login(page, username="", password="")
        _goto(page, tracking_url, timeout_ms=max(60_000, timeout_ms))
        _fill_tracking_bill_input(page, bill_code, timeout_ms=timeout_ms)
        _click_query(page, timeout_ms=timeout_ms)
        track_frame = _wait_track_frame(page, timeout_ms=timeout_ms)
        _wait_bill_loaded(track_frame, bill_code, timeout_ms=timeout_ms)

        _click_first_xpath(track_frame, SCAN_TAB_XPATHS, timeout_ms=timeout_ms)
        scan_payload = track_frame.evaluate(JS_COLLECT_SCAN_RECORDS) or {}
        route_rows = scan_payload.get("rows") if isinstance(scan_payload, dict) else []

        _click_first_xpath(track_frame, CHILD_TAB_XPATHS, timeout_ms=timeout_ms)
        track_frame.wait_for_timeout(800)
        child_payload = track_frame.evaluate(JS_COLLECT_CHILD_DISTRIBUTION) or {}
        child_rows = child_payload.get("rows") if isinstance(child_payload, dict) else []
        return (
            [row for row in route_rows or [] if isinstance(row, dict)],
            [row for row in child_rows or [] if isinstance(row, dict)],
        )
    finally:
        for obj in (context, browser):
            try:
                if obj is not None:
                    obj.close()
            except Exception:
                pass
        try:
            if p is not None:
                p.stop()
        except Exception:
            pass


def query_ronghui_tms_tracking(params: dict[str, Any]) -> dict[str, Any]:
    params = params or {}
    bill_code = _resolve_tracking_number(params)
    if not bill_code:
        return {"ok": False, "error": "缺少运单号"}
    session_profile = _clean_str(params.get("session_profile")) or "default"
    try:
        session = TMSAuth(profile=session_profile).login_and_get_session()
        tracking_url = _resolve_widget_menu_url(session)
        route_rows, child_rows, detail_row = _collect_api_tracking_rows(session, bill_code, tracking_url=tracking_url)
        if not route_rows:
            raise RonghuiTmsTrackingError("融辉 TMS 快件跟踪未返回扫描记录")
        detail_row = _overlay_decrypted_detail(params=params, bill_code=bill_code, detail_row=detail_row)
    except Exception as exc:
        return {"ok": False, "type": "ronghui_tms", "tracking_number": bill_code, "error": str(exc)}

    result = build_tracking_result(
        tracking_number=bill_code,
        route_rows=route_rows,
        detail_row=detail_row,
        child_rows=child_rows,
    )
    return result


def run_once(params: dict[str, Any]) -> dict[str, Any]:
    return query_ronghui_tms_tracking(params or {})


if __name__ == "__main__":
    import sys

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    raw = sys.stdin.read().strip()
    payload = json.loads(raw) if raw else {}
    print(json.dumps(run_once(payload), ensure_ascii=False, default=str))
