"""
Automate child-order counting for Ronghui TMS.
"""

from __future__ import annotations

import argparse
import contextvars
import datetime as _dt
import json
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, Optional, Tuple

from agent.tms_runtime.scripts.browser_manager import TMSBrowserAuth, launch_browser
from agent.tms_runtime.scripts.shared_login import load_named_accounts, resolve_primary_credentials

DEFAULT_CONFIG_PATH = os.environ.get(
    "CONFIG_PATH",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json"),
)
DEFAULT_TIMEOUT_MS = 30_000
DEFAULT_ACTION_DELAY_SEC = 1.0
DEFAULT_DUMP_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_BATCH_SIZE = 0
DEFAULT_MAX_WORKERS = 1
DEFAULT_RELOGIN_ATTEMPTS = 1
DEFAULT_DUMP_ON_ZERO = False

DEFAULT_TARGET_SITES = ("邵阳操作场", "邵阳自提部")
DEFAULT_TARGET_SITE = ",".join(DEFAULT_TARGET_SITES)
DEFAULT_KEYWORDS = ("装车", "卸车")
DEFAULT_EXCLUDE_PREFIX = "hd"

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
    "主单号",
    "主单",
    "主运单号",
    "主运单",
)

XPATH_MENU_CUSTOMER_SERVICE = '//ul/li/a/span[normalize-space(.)="客服管理"]'
XPATH_MENU_TRACKING_PRIMARY = '//ul/li/ul/li[@class="selected"]/a/span[normalize-space(.)="快件跟踪"]'
XPATH_MENU_TRACKING_FALLBACK = '//ul//li//a/span[normalize-space(.)="快件跟踪"]'

XPATH_TRACKING_IFRAME = (
    "//div[contains(@id, 'mini-') and contains(@id, '$body$')][not(contains(@style,'display'))]"
    "//iframe[starts-with(@name, 'mini-iframe-') and contains(@src, '/widget/home?authenticationKey=')]"
)
XPATH_BILL_TEXTAREA = (
    "//fieldset[@class=\"ux-fieldset\"]//span//textarea[@placeholder=\"多个单号请使用回车符分隔\"]"
)
XPATH_TRACK_IFRAME = "//iframe[@class=\"trackIframe\"]"
XPATH_TAB_CHILD_DISTRIBUTION = "//table//tr/td//table//tr/td/span[normalize-space(.)=\"子单分布\"]"

QUERY_BUTTON_XPATHS = (
    '//a[.//span[normalize-space(.)="查询"]]',
    '//a[normalize-space(.)="查询"]',
    '//span[normalize-space(.)="查询"]/ancestor::a[1]',
    '//a[.//span[normalize-space(.)="搜索"]]',
    '//a[normalize-space(.)="搜索"]',
)

_ACTION_DELAY_SEC = contextvars.ContextVar("action_delay_sec", default=DEFAULT_ACTION_DELAY_SEC)
_SPLIT_RE = re.compile(r"[,\s;]+")

JS_COUNT_SCRIPT = """
(params) => {
  const TARGET_SITES = Array.isArray(params.targetSites) && params.targetSites.length
    ? params.targetSites
    : (params.targetSite ? [params.targetSite] : ["邵阳操作场", "邵阳自提部"]);
  const KEYWORDS = Array.isArray(params.keywords) && params.keywords.length
    ? params.keywords
    : ["装车", "卸车"];
  const EXCLUDE_PREFIX = new RegExp(params.excludePrefix || "^hd", "i");
  const FIELD_ALIASES = {
    bill: [
      "BILL_CODE",
      "bill_code",
      "billCode",
      "CHILD_BILL",
      "CHILD_BILL_CODE",
      "childBill",
      "childBillCode",
      "SUB_BILL",
    "SUB_BILL_CODE",
    "subBill",
    "subBillCode",
    "WAYBILL_CODE",
    "waybillCode",
    "BILL_NO",
    "bill_no",
    "billNo",
    "CHILD_BILL_NO",
    "childBillNo",
    "SUB_BILL_NO",
    "subBillNo",
    "WAYBILL_NO",
    "waybillNo",
  ],
    site: [
      "SCAN_SITE",
      "scanSite",
      "SCAN_SITE_NAME",
      "scanSiteName",
      "SITE_NAME",
      "siteName",
      "SCAN_SITE_CODE",
      "scanSiteCode",
    ],
    type: ["SCAN_TYPE", "scanType", "SCAN_STEP", "scanStep", "OP_TYPE", "opType"],
    desc: [
      "DESCRIPTION",
      "description",
      "SCAN_DESC",
      "scanDesc",
      "SCAN_DESCRIPTION",
      "scanDescription",
      "SCAN_NOTE",
      "scanNote",
      "TRACK_DESC",
      "trackDesc",
      "SCAN_TRACK",
      "scanTrack",
    ],
  };
  const LABEL_ALIASES = {
    bill: ["子单号", "子运单号", "运单号", "单号"],
    site: ["扫描网点", "网点"],
    type: ["扫描类型", "扫描环节", "操作类型"],
    desc: ["扫描轨迹", "扫描描述", "扫描说明", "扫描备注", "扫描记录", "扫描明细"],
  };

  function normalizeText(value) {
    return value == null ? "" : String(value).trim();
  }

  function normalizeLabel(value) {
    return normalizeText(value).replace(/\s+/g, "");
  }

  function stripHtml(value) {
    return normalizeText(value).replace(/<[^>]*>/g, "");
  }

  function extractSiteFromDesc(desc) {
    const text = normalizeText(stripHtml(desc || "")).replace(/\s+/g, "");
    if (!text) return "";
    const patterns = [
      /\u5feb\u4ef6\u5728[\u3010\[]([^\u3011\]]+)[\u3011\]]/,
      /\u626b\u63cf\u7f51\u70b9(?:\u662f|\u4e3a)?[\u3010\[]([^\u3011\]]+)[\u3011\]]/,
      /\u7f51\u70b9(?:\u662f|\u4e3a)?[\u3010\[]([^\u3011\]]+)[\u3011\]]/,
    ];
    for (const re of patterns) {
      const match = text.match(re);
      if (match && match[1]) return normalizeText(match[1]);
    }
    return "";
  }

  function resolveSite(site, desc) {
    const siteText = normalizeText(site);
    if (siteText) return siteText;
    return extractSiteFromDesc(desc);
  }

  function shouldPrimeFrame(frame) {
    if (!frame || !frame.getAttribute) return false;
    const src = normalizeText(frame.getAttribute("src"));
    const dataSrc = normalizeText(frame.getAttribute("data-src"));
    if (!dataSrc) return false;
    const normalizedSrc = src.toLowerCase();
    if (normalizedSrc && normalizedSrc !== "about:blank") return false;
    return true;
  }

  function primeLazyFrame(frame) {
    try {
      if (!shouldPrimeFrame(frame)) return false;
      const dataSrc = frame.getAttribute("data-src");
      if (dataSrc) {
        frame.setAttribute("src", dataSrc);
        return true;
      }
    } catch (e) {}
    return false;
  }

  function columnLabel(col) {
    if (!col) return "";
    const raw = col.header || col.headerText || col.title || col.text || col.name || "";
    return normalizeLabel(stripHtml(raw));
  }

  function fieldMatches(field, aliases) {
    const text = normalizeText(field);
    if (!text) return false;
    const lower = text.toLowerCase();
    return aliases.some(alias => lower === String(alias).toLowerCase());
  }

  function labelMatches(label, aliases) {
    const text = normalizeLabel(label);
    if (!text) return false;
    return aliases.some(alias => text.includes(String(alias)));
  }

  function isDescLabel(label) {
    const text = normalizeLabel(label);
    if (!text || text.includes("时间")) return false;
    return LABEL_ALIASES.desc.some(alias => text.includes(String(alias)));
  }

  function getRowValueByAliases(row, aliases) {
    if (!row || !aliases || !aliases.length) return "";
    const keys = Object.keys(row);
    for (const key of keys) {
      const lower = String(key).toLowerCase();
      for (const alias of aliases) {
        if (lower === String(alias).toLowerCase()) {
          return normalizeText(row[key]);
        }
      }
    }
    return "";
  }

  function columnRole(col) {
    const field = normalizeText(col && col.field);
    const label = columnLabel(col);
    if (fieldMatches(field, FIELD_ALIASES.bill) || labelMatches(label, LABEL_ALIASES.bill)) return "bill";
    if (fieldMatches(field, FIELD_ALIASES.site) || labelMatches(label, LABEL_ALIASES.site)) return "site";
    if (fieldMatches(field, FIELD_ALIASES.type) || labelMatches(label, LABEL_ALIASES.type)) return "type";
    if (fieldMatches(field, FIELD_ALIASES.desc) || isDescLabel(label)) return "desc";
    return "";
  }

  function hasRoleCombo(roles) {
    const set = new Set((roles || []).filter(Boolean));
    return set.has("bill") && set.has("site") && (set.has("desc") || set.has("type"));
  }

  function getHeaderRoles(grid) {
    if (!grid || !grid.el) return [];
    let headerRows = [];
    try {
      headerRows = Array.from(grid.el.querySelectorAll(".mini-grid-header .mini-grid-row"));
    } catch (e) {}
    let headerCells = [];
    if (headerRows.length) {
      let bestCount = 0;
      for (const row of headerRows) {
        const cells = Array.from(row.querySelectorAll(".mini-grid-headerCell"));
        if (cells.length > bestCount) {
          bestCount = cells.length;
          headerCells = cells;
        }
      }
    }
    if (!headerCells.length) {
      try {
        headerCells = Array.from(grid.el.querySelectorAll(".mini-grid-headerCell"));
      } catch (e) {}
    }
    if (!headerCells.length) return [];
    return headerCells.map(cell => {
      const label = stripHtml(cell.innerHTML || cell.textContent || "");
      if (labelMatches(label, LABEL_ALIASES.bill)) return "bill";
      if (labelMatches(label, LABEL_ALIASES.site)) return "site";
      if (labelMatches(label, LABEL_ALIASES.type)) return "type";
      if (labelMatches(label, LABEL_ALIASES.desc) || isDescLabel(label)) return "desc";
      return "";
    });
  }

  function getGridColumns(grid) {
    if (!grid) return [];
    try {
      if (typeof grid.getBottomColumns === "function") {
        const cols = grid.getBottomColumns();
        if (cols && cols.length) return cols;
      }
    } catch (e) {}
    try {
      if (typeof grid.getColumns === "function") return grid.getColumns() || [];
    } catch (e) {}
    return [];
  }

  function pickGrid(w) {
    try {
      if (!w || !w.mini || !w.mini.get) return null;

      const g1 = w.mini.get("datagrid");
      if (isSubDistGrid(g1)) return g1;

      const ids = Array.from(w.document.querySelectorAll(".mini-datagrid"))
        .map(el => el.id)
        .filter(Boolean);

      for (const id of ids) {
        const g = w.mini.get(id);
        if (isSubDistGrid(g)) return g;
      }
    } catch (e) {}
    return null;
  }

  function isSubDistGrid(grid) {
    try {
      if (!grid || !grid.el) return false;
      const cols = getGridColumns(grid);
      if (!cols.length) return false;
      const roles = cols.map(columnRole);
      if (hasRoleCombo(roles)) return true;
      const headerRoles = getHeaderRoles(grid);
      if (hasRoleCombo(headerRoles)) return true;
      const fields = cols
        .map(c => normalizeText(c && c.field).toUpperCase())
        .filter(Boolean);
      const hasField = aliases =>
        (aliases || []).some(alias => fields.includes(String(alias).toUpperCase()));
      return (
        hasField(FIELD_ALIASES.bill) &&
        hasField(FIELD_ALIASES.site) &&
        (hasField(FIELD_ALIASES.desc) || hasField(FIELD_ALIASES.type))
      );
    } catch (e) {
      return false;
    }
  }

  function extractFromGrid(grid) {
    const columns = getGridColumns(grid);
    const columnRoles = columns.map(columnRole);
    const headerRoles = getHeaderRoles(grid);
    const rowsEl = grid.el.querySelectorAll(".mini-grid-row");
    const out = [];
    const seen = new Set();
    let parsed = 0;

    const recordRow = (bill, site, type, desc) => {
      if (bill) parsed += 1;
      if (!bill || EXCLUDE_PREFIX.test(bill)) return;
      const keywordSource = `${desc} ${type}`.trim();
      const resolvedSite = resolveSite(site, desc);
      if (!TARGET_SITES.some(s => s && resolvedSite && resolvedSite.includes(s))) return;
      if (!KEYWORDS.some(k => keywordSource.includes(k))) return;
      const key = [bill, site, type, desc].join("|");
      if (seen.has(key)) return;
      seen.add(key);
      out.push({ bill, site: resolvedSite || site, type, desc });
    };

    rowsEl.forEach(rowEl => {
      const cells = rowEl.querySelectorAll(".mini-grid-cell");

      let roles = columnRoles;
      if (headerRoles.length === cells.length) {
        roles = headerRoles;
      } else if (headerRoles.length && columnRoles.length !== cells.length) {
        roles = headerRoles;
      }

      let bill = "";
      let site = "";
      let type = "";
      let desc = "";

      cells.forEach((td, idx) => {
        const role = roles[idx];
        if (!role) return;
        const text = normalizeText(td.innerText || "");
        if (!text) return;
        if (role === "bill") bill = text;
        if (role === "site") site = text;
        if (role === "type") type = text;
        if (role === "desc") desc = text;
      });

      recordRow(bill, site, type, desc);
    });

    if (typeof grid.getData === "function") {
      const data = grid.getData() || [];
      if (data.length) {
        const roleCols = { bill: null, site: null, type: null, desc: null };
        columns.forEach((col, idx) => {
          const role = columnRoles[idx];
          if (role && !roleCols[role]) roleCols[role] = col;
        });
        const getValue = (row, col) => {
          if (!row || !col) return "";
          const field = normalizeText(col.field);
          if (field && row[field] != null) return normalizeText(row[field]);
          if (typeof grid.getCellText === "function") {
            try {
              return normalizeText(grid.getCellText(row, col));
            } catch (e) {}
          }
          return "";
        };

        for (const row of data) {
          const bill = roleCols.bill
            ? getValue(row, roleCols.bill)
            : getRowValueByAliases(row, FIELD_ALIASES.bill);
          const site = roleCols.site
            ? getValue(row, roleCols.site)
            : getRowValueByAliases(row, FIELD_ALIASES.site);
          const type = roleCols.type
            ? getValue(row, roleCols.type)
            : getRowValueByAliases(row, FIELD_ALIASES.type);
          const desc = roleCols.desc
            ? getValue(row, roleCols.desc)
            : getRowValueByAliases(row, FIELD_ALIASES.desc);

          recordRow(bill, site, type, desc);
        }
      }
    }

    return { rows: out, parsed };
  }

  function headerIndex(cells, aliases) {
    for (let i = 0; i < cells.length; i++) {
      if (labelMatches(cells[i], aliases)) return i;
    }
    return -1;
  }

  function extractFromTable(table) {
    const rows = Array.from(table.querySelectorAll("tr"));
    let headerRow = -1;
    let header = null;
    for (let i = 0; i < rows.length; i++) {
      const cells = Array.from(rows[i].querySelectorAll("th,td")).map(td =>
        normalizeText(td.innerText || td.textContent || "")
      );
      if (!cells.length) continue;
      const bill = headerIndex(cells, LABEL_ALIASES.bill);
      const site = headerIndex(cells, LABEL_ALIASES.site);
      const type = headerIndex(cells, LABEL_ALIASES.type);
      const desc = headerIndex(cells, LABEL_ALIASES.desc);
      if (bill >= 0 && site >= 0 && (desc >= 0 || type >= 0)) {
        headerRow = i;
        header = { bill, site, type, desc };
        break;
      }
    }
    if (!header) {
      return { rows: [], parsed: 0, has_header: false };
    }

    const out = [];
    let parsed = 0;
    for (let i = headerRow + 1; i < rows.length; i++) {
      const cells = Array.from(rows[i].querySelectorAll("th,td")).map(td =>
        normalizeText(td.innerText || td.textContent || "")
      );
      if (!cells.length) continue;
      const bill = normalizeText(cells[header.bill] || "");
      const site = normalizeText(cells[header.site] || "");
      const type = normalizeText(cells[header.type] || "");
      const desc = normalizeText(cells[header.desc] || "");

      if (bill) parsed += 1;
      if (!bill || EXCLUDE_PREFIX.test(bill)) continue;

      const keywordSource = `${desc} ${type}`.trim();
      const resolvedSite = resolveSite(site, desc);
      if (TARGET_SITES.some(s => s && resolvedSite && resolvedSite.includes(s)) && KEYWORDS.some(k => keywordSource.includes(k))) {
        out.push({ bill, site: resolvedSite || site, type, desc });
      }
    }

    return { rows: out, parsed, has_header: true };
  }

  function extractFromTables(doc) {
    if (!doc) return { rows: [], parsed: 0, table_count: 0 };
    const tables = Array.from(doc.querySelectorAll("table"));
    const out = [];
    let parsed = 0;
    let tableCount = 0;
    for (const table of tables) {
      const result = extractFromTable(table);
      if (result.has_header) {
        tableCount += 1;
      }
      if (result.parsed) {
        parsed += result.parsed;
      }
      if (result.rows && result.rows.length) {
        out.push(...result.rows);
      }
    }
    return { rows: out, parsed, table_count: tableCount };
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
      try {
        const frames = w.document.querySelectorAll("iframe");
        for (const f of frames) {
          try {
            primeLazyFrame(f);
          } catch (e) {}
          try {
            if (f.contentWindow) queue.push(f.contentWindow);
          } catch (e) {}
        }
      } catch (e) {}
    }
    return out;
  }

  function matchTargetSite(name) {
    const text = (name || "").trim();
    if (!text) return "";
    return TARGET_SITES.find(s => s && (text.includes(s) || s.includes(text))) || "";
  }

  function collectSummaryCounts(windows) {
    const perSite = {};
    const patterns = [
      /【\s*([^】]+?)\s*】\s*网点\s*共计\s*(\d+)\s*件/g,
      /【\s*([^】]+?)\s*】\s*共计\s*(\d+)\s*件/g,
      /([^\s\[\]【】]{2,20})\s*网点\s*共计\s*(\d+)\s*件/g,
    ];
    patterns.push(
      /【\s*([^】]+?)\s*】\s*网点\s*共计\s*(\d+)\s*件/g,
      /【\s*([^】]+?)\s*】\s*共计\s*(\d+)\s*件/g,
      /([^\s\[\]【】]{2,20})\s*网点\s*共计\s*(\d+)\s*件/g,
    );
    patterns.push(
      /\u3010\s*([^\u3011]+?)\s*\u3011\s*\u7f51\u70b9\s*\u5171\u8ba1\s*(\d+)\s*\u4ef6/g,
      /\u3010\s*([^\u3011]+?)\s*\u3011\s*\u5171\u8ba1\s*(\d+)\s*\u4ef6/g,
      /([^\s\[\]\u3010\u3011]{2,20})\s*\u7f51\u70b9\s*\u5171\u8ba1\s*(\d+)\s*\u4ef6/g,
    );

    for (const w of windows) {
      let text = "";
      try {
        text = (w.document && w.document.body && w.document.body.innerText) || "";
      } catch (e) {}
      try {
        const body = w.document && w.document.body;
        const content = (body && body.textContent) || "";
        if (content && !text.includes(content)) {
          text = `${text}\n${content}`.trim();
        }
      } catch (e) {}
      if (!text) continue;
      for (const re of patterns) {
        re.lastIndex = 0;
        let m;
        while ((m = re.exec(text)) !== null) {
          const siteName = (m[1] || "").trim();
          const count = parseInt(m[2], 10);
          if (!siteName || !Number.isFinite(count)) continue;
          const matched = matchTargetSite(siteName);
          if (!matched) continue;
          perSite[matched] = Math.max(perSite[matched] || 0, count);
        }
      }
    }

    const total = Object.values(perSite).reduce((acc, val) => acc + val, 0);
    return { found: total > 0, total, perSite };
  }

  const USE_TOP = params.useTop !== false;
  let root = window;
  if (USE_TOP) {
    try {
      if (window.top) root = window.top;
    } catch (e) {}
  }

  const windows = collectWindows(root);
  const summary = collectSummaryCounts(windows);
  const grids = [];
  const gridIds = new Set();

  for (const w of windows) {
    try {
      if (!w || !w.mini || !w.mini.get) continue;

      const ids = Array.from(w.document.querySelectorAll(".mini-datagrid"))
        .map(el => el.id)
        .filter(Boolean);

      for (const id of ids) {
        const g = w.mini.get(id);
        if (!isSubDistGrid(g)) continue;
        const gid = (g && g.id) || (g && g.el && g.el.id) || id;
        if (gid && gridIds.has(gid)) continue;
        if (gid) gridIds.add(gid);
        grids.push(g);
      }

      const g1 = w.mini.get("datagrid");
      if (isSubDistGrid(g1)) {
        const gid = (g1 && g1.id) || (g1 && g1.el && g1.el.id) || "datagrid";
        if (!gridIds.has(gid)) {
          gridIds.add(gid);
          grids.push(g1);
        }
      }
    } catch (e) {}
  }

  if (!grids.length && !summary.found) {
    return { found: false, count: 0, list: [], per_site: {}, grid_count: 0, summary_found: false };
  }

  const list = [];
  const rowByBill = new Map();
  const seen = new Set();
  let parsedRows = 0;
  let tableCount = 0;

  const rowScore = row => {
    if (!row) return 0;
    const hasTargetSite = TARGET_SITES.some(s => s && row.site && row.site.includes(s));
    const descLen = (row.desc || "").length;
    const typeLen = (row.type || "").length;
    return (hasTargetSite ? 1000 : 0) + (descLen ? 50 : 0) + (typeLen ? 5 : 0);
  };

  const mergeRow = row => {
    if (!row || !row.bill) return;
    const key = [row.bill, row.site, row.type, row.desc].join("|");
    if (seen.has(key)) return;
    seen.add(key);
    const billKey = row.bill;
    const existing = rowByBill.get(billKey);
    if (!existing || rowScore(row) > rowScore(existing)) {
      rowByBill.set(billKey, row);
    }
  };

  if (grids.length) {
    for (const g of grids) {
      const result = extractFromGrid(g);
      const rows = (result && result.rows) || [];
      parsedRows += (result && result.parsed) || 0;
      for (const row of rows) {
        mergeRow(row);
      }
    }
  }

  if (windows.length) {
    for (const w of windows) {
      let doc = null;
      try {
        doc = w.document;
      } catch (e) {}
      if (!doc) continue;
      const result = extractFromTables(doc);
      const rows = (result && result.rows) || [];
      parsedRows += (result && result.parsed) || 0;
      tableCount += (result && result.table_count) || 0;
      for (const row of rows) {
        mergeRow(row);
      }
    }
  }

  const rowPerSite = {};
  for (const row of rowByBill.values()) {
    list.push(row);
    const matchedSite = TARGET_SITES.find(s => s && row.site && row.site.includes(s)) || row.site || "";
    if (matchedSite) {
      rowPerSite[matchedSite] = (rowPerSite[matchedSite] || 0) + 1;
    }
  }

  const perSite = { ...rowPerSite };
  if (summary.found && Object.keys(rowPerSite).length === 0 && parsedRows === 0) {
    for (const [site, cnt] of Object.entries(summary.perSite)) {
      perSite[site] = Math.max(perSite[site] || 0, cnt);
    }
  }
  const count = Object.values(perSite).reduce((acc, val) => acc + val, 0);
  const countSource = Object.keys(rowPerSite).length || parsedRows > 0 ? "rows" : "summary";

  return {
    found: true,
    count,
    list,
    per_site: perSite,
    grid_count: grids.length,
    table_count: tableCount,
    summary_found: summary.found,
    count_source: countSource,
  };
}
"""

JS_SCROLL_SCRIPT = """
(params) => {
  const direction = params && params.direction === "up" ? "up" : "down";
  const useTop = !(params && params.useTop === false);

  function collectWindows(root) {
    const queue = [root];
    const seen = new Set();
    const out = [];
    while (queue.length) {
      const w = queue.shift();
      if (!w || seen.has(w)) continue;
      seen.add(w);
      out.push(w);
      try {
        const frames = w.document.querySelectorAll("iframe");
        for (const f of frames) {
          try {
            if (f.contentWindow) queue.push(f.contentWindow);
          } catch (e) {}
        }
      } catch (e) {}
    }
    return out;
  }

  function scrollElement(el) {
    if (!el) return;
    const maxTop = el.scrollHeight - el.clientHeight;
    if (maxTop <= 0) return;
    el.scrollTop = direction === "down" ? maxTop : 0;
  }

  function scrollWindow(w) {
    try {
      const body = w.document && w.document.body;
      scrollElement(body);
      const docEl = w.document && w.document.documentElement;
      scrollElement(docEl);
    } catch (e) {}

    let elements = [];
    try {
      elements = Array.from(w.document.querySelectorAll("*"));
    } catch (e) {}

    for (const el of elements) {
      try {
        const style = w.getComputedStyle(el);
        if (!style) continue;
        const overflowY = style.overflowY;
        if (overflowY !== "auto" && overflowY !== "scroll") continue;
        if (el.scrollHeight <= el.clientHeight + 20) continue;
        scrollElement(el);
      } catch (e) {}
    }
  }

  let root = window;
  if (useTop) {
    try {
      if (window.top) root = window.top;
    } catch (e) {}
  }

  const windows = collectWindows(root);
  for (const w of windows) {
    scrollWindow(w);
  }

  return true;
}
"""


def _ts() -> str:
    ts = _dt.datetime.now()
    return ts.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def log(message: str) -> None:
    print(f"[{_ts()}] {message}", flush=True)


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
    raise TimeoutError("未找到快件跟踪 iframe") from last_error


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
    raise TimeoutError("未找到快件跟踪 trackIframe") from last_error


def _collect_subtrack_frames(track_frame, *, timeout_ms: int = DEFAULT_TIMEOUT_MS):
    deadline = time.time() + timeout_ms / 1000.0
    frames = []
    seen = set()
    while time.time() < deadline:
        try:
            track_frame.evaluate(
                """
                () => {
                  document.querySelectorAll("iframe.subTrackIframe").forEach((f) => {
                    const src = (f.getAttribute("src") || "").trim();
                    const data = (f.getAttribute("data-src") || "").trim();
                    if ((!src || src === "about:blank") && data) {
                      f.setAttribute("src", data);
                    }
                  });
                }
                """
            )
        except Exception:
            pass
        try:
            iframes = track_frame.query_selector_all("iframe.subTrackIframe")
        except Exception:
            iframes = []
        for iframe in iframes:
            try:
                frame = iframe.content_frame()
            except Exception:
                frame = None
            if frame is None or frame in seen:
                continue
            seen.add(frame)
            frames.append(frame)
        if frames:
            break
        try:
            track_frame.wait_for_timeout(200)
        except Exception:
            time.sleep(0.2)
    return frames


def _row_score(row: Dict[str, Any], target_sites: List[str]) -> int:
    if not row:
        return 0
    site = str(row.get("site") or "")
    has_target = any(site and target and target in site for target in target_sites)
    desc_len = len(str(row.get("desc") or ""))
    type_len = len(str(row.get("type") or ""))
    return (1000 if has_target else 0) + (50 if desc_len else 0) + (5 if type_len else 0)


def _merge_child_lists(
    results: List[Dict[str, Any]], target_sites: List[str]
) -> Tuple[int, List[Dict[str, Any]], Dict[str, int]]:
    row_by_bill: Dict[str, Dict[str, Any]] = {}
    for result in results:
        for row in result.get("list") or []:
            bill = str(row.get("bill") or "")
            if not bill:
                continue
            existing = row_by_bill.get(bill)
            if existing is None or _row_score(row, target_sites) > _row_score(existing, target_sites):
                row_by_bill[bill] = row
    per_site: Dict[str, int] = {}
    for row in row_by_bill.values():
        site = str(row.get("site") or "")
        matched_site = next((s for s in target_sites if s and site and s in site), site or "")
        if matched_site:
            per_site[matched_site] = per_site.get(matched_site, 0) + 1
    count = sum(per_site.values())
    return count, list(row_by_bill.values()), per_site

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


def _get_first_value(raw: Dict[str, Any], keys: tuple[str, ...]) -> Optional[Any]:
    for key in keys:
        if key in raw and raw[key] is not None:
            return raw[key]
    return None


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
    bill = _get_first_value(raw, BILL_KEYS)
    bill_text = _normalize_bill_code(bill)
    if not bill_text:
        return None
    return {"bill_code": bill_text}


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


def _coerce_keywords(raw: Any) -> List[str]:
    if raw is None:
        return list(DEFAULT_KEYWORDS)
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return list(DEFAULT_KEYWORDS)
        try:
            parsed = json.loads(text)
        except Exception:
            parsed = None
        if isinstance(parsed, list):
            values = [str(item).strip() for item in parsed if str(item).strip()]
            return values or list(DEFAULT_KEYWORDS)
        values = [part.strip() for part in re.split(r"[,;\n]+", text) if part.strip()]
        return values or list(DEFAULT_KEYWORDS)
    if isinstance(raw, list):
        values = [str(item).strip() for item in raw if str(item).strip()]
        return values or list(DEFAULT_KEYWORDS)
    return list(DEFAULT_KEYWORDS)


def _coerce_target_sites(raw: Any) -> List[str]:
    if raw is None:
        return list(DEFAULT_TARGET_SITES)
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return list(DEFAULT_TARGET_SITES)
        try:
            parsed = json.loads(text)
        except Exception:
            parsed = None
        if isinstance(parsed, list):
            values = [str(item).strip() for item in parsed if str(item).strip()]
            return values or list(DEFAULT_TARGET_SITES)
        values = [part.strip() for part in re.split(r"[,;\n]+", text) if part.strip()]
        return values or list(DEFAULT_TARGET_SITES)
    if isinstance(raw, list):
        values = [str(item).strip() for item in raw if str(item).strip()]
        return values or list(DEFAULT_TARGET_SITES)
    return list(DEFAULT_TARGET_SITES)


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
            values = [str(item).strip() for item in parsed if str(item).strip()]
            return values
        values = [part.strip() for part in re.split(r"[,;\n]+", text) if part.strip()]
        return values
    if isinstance(raw, list):
        return [str(item).strip() for item in raw if str(item).strip()]
    return []


def _normalize_account_entry(raw: Any) -> Optional[Tuple[str, str]]:
    if isinstance(raw, dict) and "json" in raw and isinstance(raw["json"], dict):
        raw = raw["json"]
    if not isinstance(raw, dict):
        return None
    username = _get_first_value(raw, ("username", "user", "uid", "account", "login", "operator_uid"))
    password = _get_first_value(raw, ("password", "pwd", "pass", "operator_password"))
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


def _normalize_exclude_prefix(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        text = DEFAULT_EXCLUDE_PREFIX
    if not text.startswith("^"):
        return f"^{text}"
    return text


def _should_relogin(auth: TMSBrowserAuth, page, exc: BaseException) -> bool:
    try:
        if not auth._is_logged_in(page):  # type: ignore[attr-defined]
            return True
    except Exception:
        pass
    url = getattr(page, "url", "") or ""
    if "/system/login" in url or "#/login" in url:
        return True
    message = str(exc).lower() if exc is not None else ""
    return "login" in message or "unauthorized" in message or "forbidden" in message


def _ensure_tracking_ready(
    auth: TMSBrowserAuth,
    page,
    *,
    username: str,
    password: str,
    timeout_ms: int,
):
    if not auth._is_logged_in(page):  # type: ignore[attr-defined]
        log("session expired, re-login")
        auth.login(page, username=username, password=password)
    _open_tracking_menu(page)
    frame = _get_tracking_frame(page, timeout_ms=timeout_ms)
    _wait_xpath_visible(frame, XPATH_BILL_TEXTAREA, timeout_ms=timeout_ms)
    return frame


def _is_not_found_error(exc: BaseException) -> bool:
    message = str(exc) if exc is not None else ""
    return "未找到" in message or "not found" in message.lower()


def _safe_filename(text: str) -> str:
    keep = []
    for ch in (text or ""):
        if ch.isalnum() or ch in ("_", "-", "."):
            keep.append(ch)
        else:
            keep.append("_")
    return "".join(keep) or "child_count"


def dump_page_debug(page, frame=None, *, out_dir: str, prefix: str) -> Dict[str, str]:
    ts = _dt.datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
    safe_prefix = _safe_filename(prefix)
    unique = f"{os.getpid()}_{threading.get_ident()}"
    os.makedirs(out_dir, exist_ok=True)
    base = os.path.join(out_dir, f"{safe_prefix}_{ts}_{unique}")
    png_path = base + ".png"
    html_path = base + ".html"
    frame_path = base + ".frame.html"
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

    return results


def _open_tracking_menu(page) -> None:
    if not _is_visible(page, XPATH_MENU_TRACKING_PRIMARY) and not _is_visible(
        page, XPATH_MENU_TRACKING_FALLBACK
    ):
        _click_xpath(page, XPATH_MENU_CUSTOMER_SERVICE, label="客服管理")

    if _is_visible(page, XPATH_MENU_TRACKING_PRIMARY):
        _click_xpath(page, XPATH_MENU_TRACKING_PRIMARY, label="快件跟踪")
        return
    if _is_visible(page, XPATH_MENU_TRACKING_FALLBACK):
        _click_xpath(page, XPATH_MENU_TRACKING_FALLBACK, label="快件跟踪")
        return
    raise RuntimeError("未找到菜单：快件跟踪")


def _click_query_button(frame, *, timeout_ms: int = DEFAULT_TIMEOUT_MS) -> bool:
    for xpath in QUERY_BUTTON_XPATHS:
        if _is_visible(frame, xpath):
            _click_xpath(frame, xpath, label="查询", timeout_ms=timeout_ms)
            return True
    return False


def _search_bill_code(frame, bill_code: str, *, timeout_ms: int = DEFAULT_TIMEOUT_MS) -> None:
    normalized = _normalize_bill_code(bill_code)
    _fill_xpath(frame, XPATH_BILL_TEXTAREA, normalized, label="主单号", timeout_ms=timeout_ms)
    if not _click_query_button(frame, timeout_ms=timeout_ms):
        log("未找到查询按钮，尝试回车触发查询")
        el = _wait_xpath_visible(frame, XPATH_BILL_TEXTAREA, timeout_ms=timeout_ms)
        el.press("Enter")
        _pause(frame)


def _wait_child_count(
    scope,
    target_sites: List[str],
    keywords: List[str],
    exclude_prefix: str,
    *,
    use_top: bool = True,
    timeout_ms: int = DEFAULT_TIMEOUT_MS,
    expected_sites: Optional[int] = None,
) -> Dict[str, Any]:
    deadline = time.time() + timeout_ms / 1000.0
    params = {
        "targetSites": target_sites,
        "keywords": keywords,
        "excludePrefix": exclude_prefix,
        "useTop": bool(use_top),
    }
    if expected_sites is None:
        expected_sites = len([site for site in target_sites if site])
    else:
        expected_sites = max(0, int(expected_sites))
    max_scrolls = 4
    scrolls = 0
    best: Optional[Dict[str, Any]] = None
    last: Optional[Dict[str, Any]] = None
    while time.time() < deadline:
        try:
            value = scope.evaluate(JS_COUNT_SCRIPT, params)
            if isinstance(value, dict):
                last = value
                if not best or int(value.get("count") or 0) > int(best.get("count") or 0):
                    best = value
                per_site = value.get("per_site")
                if value.get("found") and isinstance(per_site, dict):
                    if not expected_sites or len(per_site) >= expected_sites:
                        return value
        except Exception:
            pass
        if scrolls < max_scrolls:
            scrolls += 1
            try:
                scope.evaluate(JS_SCROLL_SCRIPT, {"direction": "down", "useTop": bool(use_top)})
                scope.wait_for_timeout(500)
                scope.evaluate(JS_SCROLL_SCRIPT, {"direction": "up", "useTop": bool(use_top)})
                scope.wait_for_timeout(500)
                continue
            except Exception:
                pass
        if best and best.get("found"):
            return best
        try:
            scope.wait_for_timeout(300)
        except Exception:
            time.sleep(0.3)
    raise TimeoutError("未找到子单分布 datagrid，请确认已切到【子单分布】且表格已渲染") from None


def run_flow(
    *,
    bill_code: str,
    items: Optional[List[Dict[str, str]]],
    username: str,
    password: str,
    config_path: str,
    target_site: str,
    keywords: List[str],
    exclude_prefix: str,
    include_list: bool,
    headless: bool,
    slow_mo_ms: int,
    max_login_attempts: int,
    relogin_attempts: int,
    action_delay_sec: float,
    timeout_ms: int,
    dump_on_error: bool,
    dump_on_zero: bool,
    dump_dir: str,
) -> Dict[str, Any]:
    started = time.time()
    stage = "init"
    current_bill = ""
    results: List[Dict[str, Any]] = []
    p = browser = context = page = None
    frame = None
    delay_token = None
    try:
        delay_token = _ACTION_DELAY_SEC.set(max(0.0, float(action_delay_sec)))
        bill_items = _coerce_items(items)
        if not bill_items:
            bill_text = str(bill_code).strip()
            if not bill_text:
                raise RuntimeError("未提供主单号")
            bill_items = [{"bill_code": bill_text}]

        target_sites = _coerce_target_sites(target_site)
        target_site_text = ", ".join(target_sites)
        keyword_list = [str(k).strip() for k in (keywords or []) if str(k).strip()]
        if not keyword_list:
            keyword_list = list(DEFAULT_KEYWORDS)
        exclude_text = _normalize_exclude_prefix(exclude_prefix)

        stage = "launch_browser"
        p, browser, context, page = launch_browser(headless=headless, slow_mo_ms=slow_mo_ms)

        stage = "login"
        uid, pwd = _resolve_credentials(config_path, username=username, password=password)
        auth = TMSBrowserAuth(max_attempts=max(1, int(max_login_attempts)))
        log("开始登录")
        auth.login(page, username=uid, password=pwd)
        log(f"登录完成，当前URL：{page.url}")

        stage = "open_menu"
        frame = _ensure_tracking_ready(auth, page, username=uid, password=pwd, timeout_ms=timeout_ms)

        for item in bill_items:
            bill = _normalize_bill_code(item.get("bill_code"))
            if not bill:
                continue
            current_bill = bill

            attempts = max(1, int(relogin_attempts) + 1)
            for attempt in range(attempts):
                try:
                    stage = "query_bill"
                    if attempt > 0:
                        log(f"retry bill {bill}, attempt {attempt + 1}/{attempts}")
                    frame = _ensure_tracking_ready(auth, page, username=uid, password=pwd, timeout_ms=timeout_ms)
                    log(f"query bill: {bill}")
                    _search_bill_code(frame, bill, timeout_ms=timeout_ms)

                    stage = "open_child_tab"
                    track_frame = _get_track_frame(frame, timeout_ms=timeout_ms)
                    _click_xpath(
                        track_frame,
                        XPATH_TAB_CHILD_DISTRIBUTION,
                        label="child distribution",
                        timeout_ms=timeout_ms,
                    )

                    stage = "count_children"
                    count_result = _wait_child_count(
                        track_frame,
                        target_sites,
                        keyword_list,
                        exclude_text,
                        use_top=False,
                        timeout_ms=timeout_ms,
                    )
                    base_list = count_result.get("list") or []
                    per_site = count_result.get("per_site") or {}
                    target_expected = [site for site in target_sites if site]
                    missing_sites = [site for site in target_expected if site not in per_site]
                    needs_subframes = not base_list or bool(missing_sites)
                    if needs_subframes:
                        subframes = _collect_subtrack_frames(track_frame, timeout_ms=timeout_ms)
                        if subframes:
                            log(f"subTrackIframe fallback: {len(subframes)} frame(s)")
                        sub_results: List[Dict[str, Any]] = []
                        for subframe in subframes:
                            try:
                                sub_result = _wait_child_count(
                                    subframe,
                                    target_sites,
                                    keyword_list,
                                    exclude_text,
                                    use_top=False,
                                    timeout_ms=timeout_ms,
                                    expected_sites=0,
                                )
                            except Exception as sub_exc:
                                log(f"subframe count failed: {type(sub_exc).__name__}: {sub_exc}")
                                continue
                            if sub_result.get("list"):
                                sub_results.append(sub_result)
                        if sub_results:
                            merged_count, merged_list, per_site = _merge_child_lists(
                                [count_result] + sub_results, target_sites
                            )
                            if merged_list:
                                count_result = {
                                    **count_result,
                                    "count": merged_count,
                                    "list": merged_list,
                                    "per_site": per_site,
                                    "count_source": "subframes",
                                }
                except BaseException as exc:
                    if _is_not_found_error(exc):
                        record = {"bill_code": bill, "count": 0}
                        if include_list:
                            record["list"] = []
                        results.append(record)
                        log(f"child_count not found for {bill}, return 0")
                        break
                    if attempt < attempts - 1 and _should_relogin(auth, page, exc):
                        stage = "relogin"
                        log(f"session lost, relogin and retry {bill}")
                        try:
                            auth.login(page, username=uid, password=pwd)
                        except Exception as login_exc:
                            log(f"relogin failed: {type(login_exc).__name__}: {login_exc}")
                        continue
                    raise

                count = int(count_result.get("count") or 0)
                if dump_on_zero and count == 0 and page is not None:
                    try:
                        zero_debug = dump_page_debug(
                            page,
                            frame=track_frame,
                            out_dir=dump_dir or DEFAULT_DUMP_DIR,
                            prefix=f"child_count_zero_{bill}",
                        )
                        if zero_debug:
                            log(f"zero count debug saved: {zero_debug}")
                    except Exception as dump_exc:
                        log(f"zero count debug failed: {type(dump_exc).__name__}: {dump_exc}")
                record: Dict[str, Any] = {"bill_code": bill, "count": count}
                if include_list:
                    record["list"] = count_result.get("list") or []
                per_site = count_result.get("per_site")
                if isinstance(per_site, dict) and per_site:
                    record["per_site"] = per_site
                count_source = count_result.get("count_source")
                if count_source:
                    record["count_source"] = count_source
                results.append(record)
                log(f"bill {bill} child count: {count}")
                break

        stage = "done"
        total_hits = sum(item.get("count", 0) for item in results)

        return {
            "ok": True,
            "stage": stage,
            "message": "success",
            "detail": {
                "target_site": target_site_text,
                "keywords": list(keyword_list),
                "exclude_prefix": exclude_text,
                "results": results,
                "total_bills": len(results),
                "total_hits": total_hits,
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
                    prefix=f"child_count_{stage}",
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
                "current_bill": current_bill,
                "results": results,
                "target_site": target_site_text,
                "keywords": keywords,
                "exclude_prefix": exclude_prefix,
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


def run_flow_parallel(
    *,
    bill_code: str,
    items: Optional[List[Dict[str, str]]],
    username: str,
    password: str,
    config_path: str,
    target_site: str,
    keywords: List[str],
    exclude_prefix: str,
    include_list: bool,
    headless: bool,
    slow_mo_ms: int,
    max_login_attempts: int,
    relogin_attempts: int,
    action_delay_sec: float,
    timeout_ms: int,
    dump_on_error: bool,
    dump_on_zero: bool,
    dump_dir: str,
    batch_size: int,
    max_workers: int,
    accounts: Optional[List[Tuple[str, str]]] = None,
) -> Dict[str, Any]:
    started = time.time()
    bill_items = _coerce_items(items)
    if not bill_items:
        bill_text = str(bill_code).strip()
        if not bill_text:
            raise RuntimeError("missing bill code")
        bill_items = [{"bill_code": bill_text}]

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
            target_site=target_site,
            keywords=keywords,
            exclude_prefix=exclude_prefix,
            include_list=include_list,
            headless=headless,
            slow_mo_ms=slow_mo_ms,
            max_login_attempts=max_login_attempts,
            relogin_attempts=relogin_attempts,
            action_delay_sec=action_delay_sec,
            timeout_ms=timeout_ms,
            dump_on_error=dump_on_error,
            dump_on_zero=dump_on_zero,
            dump_dir=dump_dir,
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
            target_site=target_site,
            keywords=keywords,
            exclude_prefix=exclude_prefix,
            include_list=include_list,
            headless=headless,
            slow_mo_ms=slow_mo_ms,
            max_login_attempts=max_login_attempts,
            relogin_attempts=relogin_attempts,
            action_delay_sec=action_delay_sec,
            timeout_ms=timeout_ms,
            dump_on_error=dump_on_error,
            dump_on_zero=dump_on_zero,
            dump_dir=dump_dir,
        )

    def _run_batch(batch_index: int, batch_items: List[Dict[str, str]]) -> Tuple[int, Dict[str, Any]]:
        if accounts:
            account = accounts[batch_index % len(accounts)]
            batch_user, batch_pwd = account
        else:
            batch_user, batch_pwd = username, password
        try:
            result = run_flow(
                bill_code="",
                items=batch_items,
                username=batch_user,
                password=batch_pwd,
                config_path=config_path,
                target_site=target_site,
                keywords=keywords,
                exclude_prefix=exclude_prefix,
                include_list=include_list,
                headless=headless,
                slow_mo_ms=slow_mo_ms,
                max_login_attempts=max_login_attempts,
                relogin_attempts=relogin_attempts,
                action_delay_sec=action_delay_sec,
                timeout_ms=timeout_ms,
                dump_on_error=dump_on_error,
                dump_on_zero=dump_on_zero,
                dump_dir=dump_dir,
            )
        except BaseException as exc:
            result = {
                "ok": False,
                "stage": "batch_failed",
                "message": f"{type(exc).__name__}: {exc}",
                "detail": {"results": []},
            }
        return batch_index, result

    futures = []
    results_by_index: List[Tuple[int, Dict[str, Any]]] = []
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        for index, batch in enumerate(batches):
            futures.append(executor.submit(_run_batch, index, batch))
        for future in as_completed(futures):
            results_by_index.append(future.result())

    results_by_index.sort(key=lambda item: item[0])
    merged_results: List[Dict[str, Any]] = []
    batch_errors: List[Dict[str, Any]] = []
    urls: List[str] = []
    total_hits = 0
    all_ok = True
    for batch_index, batch_result in results_by_index:
        batch_ok = bool(batch_result.get("ok"))
        all_ok = all_ok and batch_ok
        detail = batch_result.get("detail") or {}
        batch_list = detail.get("results") or []
        for record in batch_list:
            merged_results.append(record)
        total_hits += sum(item.get("count", 0) for item in batch_list)
        url = detail.get("url")
        if url:
            urls.append(url)
        if not batch_ok:
            batch_errors.append(
                {
                    "batch_index": batch_index,
                    "stage": batch_result.get("stage"),
                    "message": batch_result.get("message"),
                }
            )

    stage = "done" if all_ok else "partial"
    return {
        "ok": all_ok,
        "stage": stage,
        "message": "success" if all_ok else "partial success",
        "detail": {
            "target_site": ", ".join(_coerce_target_sites(target_site)),
            "keywords": list(keywords or []),
            "exclude_prefix": _normalize_exclude_prefix(exclude_prefix),
            "results": merged_results,
            "total_bills": len(merged_results),
            "total_hits": total_hits,
            "urls": urls,
            "batch_size": batch_size,
            "batch_count": len(batches),
            "max_workers": max_workers,
            "accounts_used": len(accounts),
            "batch_errors": batch_errors,
        },
        "ts": _ts(),
        "cost_sec": round(time.time() - started, 3),
    }


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Ronghui TMS child-order counter (Playwright).")
    parser.add_argument("--bill-code", default="", help="主单号")
    parser.add_argument("--bill-codes", default="", help="多个主单号，逗号/空格/换行分隔")
    parser.add_argument("--items-json", default="", help="JSON 列表/对象")
    parser.add_argument("--items-file", default="", help="包含 JSON 列表的文件路径")
    parser.add_argument("--username", default="", help="账号（为空时读取 config.json）")
    parser.add_argument("--password", default="", help="密码（为空时读取 config.json）")
    parser.add_argument("--config-path", default=DEFAULT_CONFIG_PATH, help="config.json 路径")
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE, help="batch size for parallel run")
    parser.add_argument("--max-workers", type=int, default=DEFAULT_MAX_WORKERS, help="max parallel workers")
    parser.add_argument("--relogin-attempts", type=int, default=DEFAULT_RELOGIN_ATTEMPTS, help="relogin retries per bill")
    parser.add_argument("--accounts-json", default="", help="accounts list json")
    parser.add_argument("--account-keys", default="", help="config account keys, split by comma")
    parser.add_argument("--target-site", default=DEFAULT_TARGET_SITE, help="目标网点名称（支持多个，逗号/换行分隔）")
    parser.add_argument("--keywords", default="", help="关键字，支持逗号/换行/JSON 数组")
    parser.add_argument("--exclude-prefix", default=DEFAULT_EXCLUDE_PREFIX, help="排除前缀（默认 hd）")
    parser.add_argument(
        "--include-list",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="输出命中明细列表",
    )
    parser.add_argument(
        "--headless",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="无头模式运行（默认无头）",
    )
    parser.add_argument("--slow-mo-ms", type=int, default=0, help="每步操作延迟毫秒")
    parser.add_argument("--max-login-attempts", type=int, default=6, help="登录重试次数")
    parser.add_argument("--timeout-ms", type=int, default=DEFAULT_TIMEOUT_MS, help="单步超时时间毫秒")
    parser.add_argument("--action-delay-sec", type=float, default=DEFAULT_ACTION_DELAY_SEC, help="动作间延迟秒数")
    parser.add_argument("--dump-on-error", action="store_true", help="失败时保存截图/HTML")
    parser.add_argument(
        "--dump-on-zero",
        action="store_true",
        default=DEFAULT_DUMP_ON_ZERO,
        help="save debug files when count is zero",
    )
    parser.add_argument("--dump-dir", default=DEFAULT_DUMP_DIR, help="调试输出目录")
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
    keywords = _coerce_keywords(args.keywords)
    accounts = _coerce_accounts(args.accounts_json)
    if not accounts:
        account_keys = _coerce_account_keys(args.account_keys)
        accounts = _load_accounts_from_config(str(args.config_path), account_keys)

    result = run_flow_parallel(
        bill_code=str(args.bill_code),
        items=items,
        username=str(args.username),
        password=str(args.password),
        config_path=str(args.config_path),
        target_site=str(args.target_site),
        keywords=keywords,
        exclude_prefix=str(args.exclude_prefix),
        include_list=bool(args.include_list),
        headless=bool(args.headless),
        slow_mo_ms=int(args.slow_mo_ms),
        max_login_attempts=int(args.max_login_attempts),
        relogin_attempts=int(args.relogin_attempts),
        action_delay_sec=float(args.action_delay_sec),
        timeout_ms=int(args.timeout_ms),
        dump_on_error=bool(args.dump_on_error),
        dump_on_zero=bool(args.dump_on_zero),
        dump_dir=str(args.dump_dir),
        batch_size=int(args.batch_size),
        max_workers=int(args.max_workers),
        accounts=accounts,
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
            "主单号",
            default="",
        )
        items = _coerce_items(single)

    keywords = _coerce_keywords(_get_param(params, "keywords", "keyword", default=None))

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
        target_site=str(_get_param(params, "target_site", "targetSite", default=DEFAULT_TARGET_SITE)),
        keywords=keywords,
        exclude_prefix=str(_get_param(params, "exclude_prefix", "excludePrefix", default=DEFAULT_EXCLUDE_PREFIX)),
        include_list=_coerce_bool(_get_param(params, "include_list", "includeList", default=False)),
        headless=_coerce_bool(_get_param(params, "headless", default=True)),
        slow_mo_ms=int(_get_param(params, "slow_mo_ms", "slowMoMs", default=0)),
        max_login_attempts=int(_get_param(params, "max_login_attempts", "maxLoginAttempts", default=6)),
        relogin_attempts=int(
            _get_param(params, "relogin_attempts", "reloginAttempts", default=DEFAULT_RELOGIN_ATTEMPTS)
        ),
        action_delay_sec=float(
            _get_param(params, "action_delay_sec", "actionDelaySec", default=DEFAULT_ACTION_DELAY_SEC)
        ),
        timeout_ms=int(_get_param(params, "timeout_ms", "timeoutMs", default=DEFAULT_TIMEOUT_MS)),
        dump_on_error=_coerce_bool(_get_param(params, "dump_on_error", "dumpOnError", default=True)),
        dump_on_zero=_coerce_bool(
            _get_param(params, "dump_on_zero", "dumpOnZero", default=DEFAULT_DUMP_ON_ZERO)
        ),
        dump_dir=str(_get_param(params, "dump_dir", "dumpDir", default=DEFAULT_DUMP_DIR)),
        batch_size=int(_get_param(params, "batch_size", "batchSize", default=DEFAULT_BATCH_SIZE)),
        max_workers=int(_get_param(params, "max_workers", "maxWorkers", default=DEFAULT_MAX_WORKERS)),
        accounts=accounts,
    )


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
