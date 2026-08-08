# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, Optional
from urllib.parse import unquote

from agent.tms_runtime.scripts.browser_manager import TMSBrowserAuth, launch_browser

MODULE_DIR = os.path.dirname(os.path.abspath(__file__))
LOGIN_URL = "https://tms.ronghuiwl.com/system/login"
INDEX_URL = "https://tms.ronghuiwl.com/module/index?mv=index"
ORDER_ENTRY_MENU_ID = "1622"
ADDRESS_INPUT_SELECTOR = "input[id='ACCEPT_MAN_ADDRESS$text']"
ACCEPT_NAME_SELECTOR = "input[id='ACCEPT_MAN$text']"
USER_INFO_FIELDS = (
    "loginEmpCode",
    "loginEmpName",
    "loginEmpType",
    "loginOwnerFinanceCode",
    "loginOwnerSiteCode",
    "loginSiteCode",
    "loginSiteName",
    "loginSiteProvinceCode",
    "loginSiteType",
    "loginUserAccount",
    "loginUserId",
    "loginUserName",
)


class BrowserAddressResolver:
    def __init__(
        self,
        *,
        username: str = "",
        password: str = "",
        config_path: str = "",
    ) -> None:
        self.username = username
        self.password = password
        self.config_path = config_path
        self._playwright = None
        self._browser = None
        self._context = None
        self._page = None
        self._frame = None
        self._worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ronghui-address-resolver")
        self._worker_thread_id: Optional[int] = None
        self._worker_closed = False

    def close(self) -> None:
        if self._worker_closed:
            return
        try:
            if threading.get_ident() == self._worker_thread_id:
                self._close_browser()
            else:
                self._run_on_worker(self._close_browser)
        finally:
            self._worker_closed = True
            self._worker.shutdown(wait=threading.get_ident() != self._worker_thread_id)

    def _run_on_worker(self, fn, *args):
        if threading.get_ident() == self._worker_thread_id:
            return fn(*args)
        if self._worker_closed:
            raise RuntimeError("browser address resolver is closed")
        return self._worker.submit(self._call_on_worker, fn, *args).result()

    def _call_on_worker(self, fn, *args):
        self._worker_thread_id = threading.get_ident()
        return fn(*args)

    def _close_browser(self) -> None:
        if self._browser is not None:
            try:
                self._browser.close()
            except Exception:
                pass
        if self._playwright is not None:
            try:
                self._playwright.stop()
            except Exception:
                pass
        self._playwright = None
        self._browser = None
        self._context = None
        self._page = None
        self._frame = None

    def resolve(self, address: str) -> Dict[str, str]:
        return self._run_on_worker(self._resolve, address)

    def _resolve(self, address: str) -> Dict[str, str]:
        last_error: Optional[Exception] = None
        for _ in range(2):
            try:
                self._ensure_ready()
                return self._resolve_once(address)
            except Exception as exc:
                last_error = exc
                self._close_browser()
        raise RuntimeError(f"browser address resolve failed: {last_error}")

    def _ensure_ready(self) -> None:
        if self._frame is not None:
            try:
                if self._frame.locator(ADDRESS_INPUT_SELECTOR).is_visible(timeout=1000):
                    return
            except Exception:
                pass
        self._close_browser()
        self._login_and_open_tab()

    def _login_and_open_tab(self) -> None:
        self._playwright, self._browser, self._context, self._page = launch_browser(headless=True, profile="price")
        auth = TMSBrowserAuth(
            base_url="https://tms.ronghuiwl.com",
            login_url=LOGIN_URL,
            home_url=INDEX_URL,
            profile="price",
        )
        auth.login(self._page, username=self.username, password=self.password)

        menu = self._page.evaluate(
            """async () => (await (await fetch('/menuTreeExtend/loadMenu', {method:'POST'})).json())"""
        )
        menu_url = self._walk_menu(menu.get("result", {}).get("data") or [])
        if not menu_url:
            raise RuntimeError("browser resolver could not find 运单录入 menu")

        self._page.evaluate(
            """
            (item) => {
              const tabs = mini.get('mainTabs');
              let tab = tabs.getTab('tab$1622');
              if (!tab) {
                tab = {
                  name: 'tab$1622',
                  title: '运单录入',
                  showCloseButton: true,
                  url: item.url,
                  iconCls: item.iconCls || ''
                };
                tabs.addTab(tab);
              }
              tabs.activeTab(tab);
            }
            """,
            {"url": menu_url, "iconCls": "fa fa-paper-plane"},
        )

        for _ in range(30):
            self._page.wait_for_timeout(1000)
            for frame in self._page.frames:
                try:
                    if frame.locator(ADDRESS_INPUT_SELECTOR).is_visible(timeout=1000):
                        self._frame = frame
                        return
                except Exception:
                    continue
        raise RuntimeError("browser resolver could not load 运单录入 iframe")

    def _walk_menu(self, nodes) -> Optional[str]:
        for node in nodes:
            text = str(node.get("text", "") or "")
            if str(node.get("id", "")) == ORDER_ENTRY_MENU_ID or "运单录入" in text:
                return node.get("url")
            child = self._walk_menu(node.get("children") or [])
            if child:
                return child
        return None

    def _wait_page_ready(self) -> None:
        if self._page is None:
            raise RuntimeError("browser resolver page not ready")
        self._page.wait_for_function(
            """
            () => Array.from(document.querySelectorAll('div[id^="__modalmini-"]'))
              .every(el => getComputedStyle(el).display === 'none' || el.offsetParent === null)
            """,
            timeout=30000,
        )

    @staticmethod
    def _decode_js_cookie_value(value: Any) -> str:
        text = str(value or "").strip()
        if not text:
            return ""

        def replace_unicode_escape(match: re.Match[str]) -> str:
            try:
                return chr(int(match.group(1), 16))
            except Exception:
                return match.group(0)

        text = re.sub(r"%u([0-9A-Fa-f]{4})", replace_unicode_escape, text)
        return unquote(text)

    @classmethod
    def _parse_user_info_cookie(cls, value: Any) -> Dict[str, Any]:
        raw = str(value or "").strip()
        if not raw:
            return {}
        for candidate in (raw, cls._decode_js_cookie_value(raw)):
            if not candidate:
                continue
            try:
                data = json.loads(candidate)
            except Exception:
                continue
            if isinstance(data, dict):
                return data
        return {}

    @staticmethod
    def _sanitize_user_info(value: Dict[str, Any]) -> Dict[str, str]:
        if not isinstance(value, dict):
            return {}
        return {
            field: str(value.get(field) or "")
            for field in USER_INFO_FIELDS
            if value.get(field) is not None
        }

    def _load_page_user_info(self) -> Dict[str, str]:
        for loader in (self._load_page_user_info_from_session, self._load_page_user_info_from_context):
            try:
                user_info = loader()
            except Exception:
                user_info = {}
            sanitized = self._sanitize_user_info(user_info)
            if sanitized.get("loginEmpName") and sanitized.get("loginEmpCode"):
                return sanitized
        return {}

    def _load_page_user_info_from_session(self) -> Dict[str, Any]:
        try:
            from agent.tms_runtime.session_broker import get_session_broker
        except Exception:
            return {}
        session = get_session_broker("price").build_requests_session(validate=True)
        return self._parse_user_info_cookie(session.cookies.get("userInfo"))

    def _load_page_user_info_from_context(self) -> Dict[str, Any]:
        if self._context is None:
            return {}
        cookies = self._context.cookies(BASE_ORIGIN)
        for cookie in cookies or []:
            if isinstance(cookie, dict) and cookie.get("name") == "userInfo":
                return self._parse_user_info_cookie(cookie.get("value"))
        return {}

    def _prepare_entry_page_context(self) -> None:
        if self._frame is None:
            raise RuntimeError("browser resolver frame not ready")
        result = self._frame.evaluate(
            """
            async (userInfo) => {
                const currentUserInfo = (() => {
                    try {
                        return window.$Z && $Z.user && $Z.user.getUserInfo
                            ? $Z.user.getUserInfo()
                            : null;
                    } catch (_err) {
                        return null;
                    }
                })();
                const mergedUserInfo = Object.assign({}, currentUserInfo || {}, userInfo || {});
                if (mergedUserInfo && mergedUserInfo.loginEmpName) {
                    if (!window.$Z) {
                        window.$Z = {};
                    }
                    if (!$Z.user) {
                        $Z.user = {};
                    }
                    const originalGetUserInfo = typeof $Z.user.getUserInfo === 'function'
                        ? $Z.user.getUserInfo.bind($Z.user)
                        : null;
                    $Z.user.getUserInfo = () => {
                        let liveUserInfo = {};
                        if (originalGetUserInfo) {
                            try {
                                liveUserInfo = originalGetUserInfo() || {};
                            } catch (_err) {
                                liveUserInfo = {};
                            }
                        }
                        return Object.assign({}, liveUserInfo, mergedUserInfo);
                    };
                    $Z.user.getUserId = () => mergedUserInfo.loginUserId || mergedUserInfo.loginEmpCode || '';
                }
                window.L = window.L || {};
                window.L.icon = typeof window.L.icon === 'function' ? window.L.icon : (() => ({}));
                window.L.marker = typeof window.L.marker === 'function' ? window.L.marker : (() => ({}));
                window.L.polygon = typeof window.L.polygon === 'function' ? window.L.polygon : (() => ({}));
                window.editableLayers = window.editableLayers || {};
                window.editableLayers.clearLayers = typeof window.editableLayers.clearLayers === 'function'
                    ? window.editableLayers.clearLayers
                    : (() => {});
                window.editableLayers.addLayer = typeof window.editableLayers.addLayer === 'function'
                    ? window.editableLayers.addLayer
                    : (() => {});
                window.map = window.map || {};
                window.map.setView = typeof window.map.setView === 'function' ? window.map.setView : (() => {});
                window.map.setZoom = typeof window.map.setZoom === 'function' ? window.map.setZoom : (() => {});
                const normalizeRows = (payload) => {
                    if (Array.isArray(payload)) return payload;
                    if (payload && Array.isArray(payload.data)) return payload.data;
                    if (payload && payload.result && Array.isArray(payload.result.data)) return payload.result.data;
                    if (payload && Array.isArray(payload.result)) return payload.result;
                    return [];
                };
                const controlRows = (control) => {
                    try {
                        if (control && typeof control.getData === 'function') {
                            const data = control.getData();
                            if (Array.isArray(data)) return data;
                        }
                    } catch (_err) {}
                    for (const key of ['data', '_data']) {
                        if (control && Array.isArray(control[key])) return control[key];
                    }
                    return [];
                };
                const rowValues = (row) => {
                    if (!row || typeof row !== 'object') return [];
                    return [
                        row.LEVELS,
                        row.VALUE,
                        row.value,
                        row.CODE,
                        row.code,
                        row.ID,
                        row.id,
                        row.TEXT,
                        row.text,
                        row.LABEL,
                        row.label,
                        row.NAME,
                        row.name,
                    ].map((value) => String(value == null ? '' : value).trim()).filter(Boolean);
                };
                const normalizeSiteLevelsRow = (row) => {
                    if (!row || typeof row !== 'object') return row;
                    const level = row.LEVELS ?? row.VALUE ?? row.value ?? row.CODE ?? row.code ?? row.ID ?? row.id ?? row.TEXT ?? row.text ?? row.LABEL ?? row.label ?? row.NAME ?? row.name;
                    const text = row.TEXT ?? row.text ?? row.LABEL ?? row.label ?? row.NAME ?? row.name ?? row.LEVELS ?? row.VALUE ?? row.value ?? level;
                    if (level == null || String(level).trim() === '') return row;
                    return Object.assign({}, row, {
                        LEVELS: String(level).trim(),
                        VALUE: row.VALUE ?? level,
                        value: row.value ?? level,
                        TEXT: text == null ? String(level).trim() : String(text).trim(),
                        text: text == null ? String(level).trim() : String(text).trim(),
                        NAME: row.NAME ?? text ?? level,
                        name: row.name ?? text ?? level,
                    });
                };
                const findSiteLevelsRow = (rows, desiredValues) => {
                    const wanted = desiredValues
                        .map((value) => String(value == null ? '' : value).trim())
                        .filter(Boolean);
                    if (!wanted.length) return null;
                    const row = rows.find((row) => {
                        const values = rowValues(row);
                        return wanted.some((value) => values.includes(value));
                    }) || null;
                    return normalizeSiteLevelsRow(row);
                };
                const buildSiteLevelsRow = (level) => {
                    const value = String(level == null ? '' : level).trim();
                    if (!value) return null;
                    return {
                        LEVELS: value,
                        VALUE: value,
                        value,
                        TEXT: value,
                        text: value,
                        LABEL: value,
                        label: value,
                        NAME: value,
                        name: value,
                    };
                };
                const loadSiteLevelsRows = async () => {
                    try {
                        const response = await fetch('/minic/combobox?optionCode=SITE_LEVELS', {
                            credentials: 'include',
                            headers: {'X-Requested-With': 'XMLHttpRequest'}
                        });
                        return {ok: true, rows: normalizeRows(await response.json())};
                    } catch (err) {
                        return {
                            ok: false,
                            rows: [],
                            error: 'SITE_LEVELS load failed: ' + (err && err.message ? err.message : err)
                        };
                    }
                };
                const createSyntheticSiteLevelsControl = (initialRows) => {
                    let rows = Array.isArray(initialRows) ? initialRows.map(normalizeSiteLevelsRow) : [];
                    let selectedRow = null;
                    let currentValue = '';
                    let currentText = '';
                    const control = {
                        valueField: 'LEVELS',
                        textField: 'TEXT',
                        getData: () => rows,
                        setData: (nextRows) => {
                            if (Array.isArray(nextRows)) rows = nextRows.map(normalizeSiteLevelsRow);
                        },
                        getValueField: () => control.valueField,
                        getTextField: () => control.textField,
                        getValue: () => currentValue,
                        setValue: (value) => {
                            currentValue = String(value == null ? '' : value);
                        },
                        setText: (value) => {
                            currentText = String(value == null ? '' : value);
                        },
                        select: (row) => {
                            selectedRow = row || null;
                            if (!row) return;
                            const value = row.LEVELS ?? row.VALUE ?? row.value ?? row.CODE ?? row.code ?? row.ID ?? row.id;
                            const text = row.TEXT ?? row.text ?? row.NAME ?? row.name ?? row.LEVELS ?? value;
                            if (value != null) currentValue = String(value);
                            if (text != null) currentText = String(text);
                        },
                        getSelected: () => selectedRow,
                    };
                    window.__ronghuiSyntheticSiteLevels = control;
                    if (window.mini && typeof mini.get === 'function' && !mini.__ronghuiSiteLevelsPatched) {
                        const originalMiniGet = mini.get.bind(mini);
                        mini.get = (id) => {
                            const realControl = originalMiniGet(id);
                            if (id === 'SITE_LEVELS' && !realControl && window.__ronghuiSyntheticSiteLevels) {
                                return window.__ronghuiSyntheticSiteLevels;
                            }
                            return realControl;
                        };
                        mini.__ronghuiSiteLevelsPatched = true;
                    }
                    return control;
                };
                const selectSiteLevelsRow = (control, row) => {
                    row = normalizeSiteLevelsRow(row);
                    if (!control || !row || row.LEVELS == null) return false;
                    if (typeof control.setData === 'function') {
                        const rows = controlRows(control);
                        if (!rows.length) {
                            try {
                                control.setData([row]);
                            } catch (_err) {}
                        }
                    }
                    const valueField = (
                        typeof control.getValueField === 'function' ? control.getValueField() : ''
                    ) || control.valueField || control._valueField || 'id';
                    const textField = (
                        typeof control.getTextField === 'function' ? control.getTextField() : ''
                    ) || control.textField || control._textField || 'text';
                    const value = row[valueField] ?? row.VALUE ?? row.value ?? row.CODE ?? row.code ?? row.ID ?? row.id ?? row.LEVELS;
                    const text = row[textField] ?? row.TEXT ?? row.text ?? row.NAME ?? row.name ?? row.LEVELS;
                    try {
                        if (typeof control.setValue === 'function' && value != null) control.setValue(String(value));
                    } catch (_err) {}
                    try {
                        if (typeof control.setText === 'function' && text != null) control.setText(String(text));
                    } catch (_err) {}
                    try {
                        if (typeof control.select === 'function') control.select(row);
                    } catch (_err) {}
                    const originalGetSelected = typeof control.getSelected === 'function'
                        ? control.getSelected.bind(control)
                        : null;
                    const selected = originalGetSelected ? originalGetSelected() : null;
                    if (selected && selected.LEVELS != null) return true;
                    control.getSelected = () => {
                        const liveSelected = originalGetSelected ? originalGetSelected() : null;
                        return liveSelected && liveSelected.LEVELS != null ? liveSelected : row;
                    };
                    return true;
                };
                const ensureSiteLevels = async () => {
                    let siteLevels = window.mini && mini.get && mini.get('SITE_LEVELS');
                    const desiredLevel = String(mergedUserInfo.loginSiteType || '').trim();
                    if (!siteLevels) {
                        const loaded = await loadSiteLevelsRows();
                        if (!loaded.ok) return loaded;
                        let row = findSiteLevelsRow(loaded.rows, [desiredLevel]);
                        if (!row && desiredLevel && !loaded.rows.length) {
                            row = buildSiteLevelsRow(desiredLevel);
                        }
                        if (!row) {
                            return {
                                ok: false,
                                error: desiredLevel
                                    ? 'SITE_LEVELS option missing for loginSiteType=' + desiredLevel
                                    : 'SITE_LEVELS control missing and loginSiteType missing'
                            };
                        }
                        const rows = loaded.rows.length ? loaded.rows : [row];
                        siteLevels = createSyntheticSiteLevelsControl(rows);
                        if (!selectSiteLevelsRow(siteLevels, row)) {
                            return {ok: false, error: 'SITE_LEVELS selection missing'};
                        }
                        return {ok: true, error: ''};
                    }
                    let rows = controlRows(siteLevels);
                    if (!rows.length) {
                        const loaded = await loadSiteLevelsRows();
                        if (!loaded.ok) return loaded;
                        rows = loaded.rows;
                        if (rows.length && typeof siteLevels.setData === 'function') {
                            siteLevels.setData(rows);
                        }
                    }
                    const selected = typeof siteLevels.getSelected === 'function' ? siteLevels.getSelected() : null;
                    if (selected && selected.LEVELS != null) {
                        return {ok: true, error: ''};
                    }
                    const currentValue = typeof siteLevels.getValue === 'function' ? siteLevels.getValue() : '';
                    const row = findSiteLevelsRow(rows, [desiredLevel, currentValue]);
                    if (!row) {
                        return {
                            ok: false,
                            error: desiredLevel
                                ? 'SITE_LEVELS option missing for loginSiteType=' + desiredLevel
                                : 'loginSiteType missing for SITE_LEVELS selection'
                        };
                    }
                    if (!selectSiteLevelsRow(siteLevels, row)) {
                        return {ok: false, error: 'SITE_LEVELS selection missing'};
                    }
                    const prepared = typeof siteLevels.getSelected === 'function' ? siteLevels.getSelected() : null;
                    return {
                        ok: Boolean(prepared && prepared.LEVELS != null),
                        error: prepared && prepared.LEVELS != null ? '' : 'SITE_LEVELS selection missing'
                    };
                };
                const siteLevelsResult = await ensureSiteLevels();
                const preparedUserInfo = (() => {
                    try {
                        return window.$Z && $Z.user && $Z.user.getUserInfo
                            ? $Z.user.getUserInfo()
                            : null;
                    } catch (_err) {
                        return null;
                    }
                })();
                return {
                    has_user_info: Boolean(preparedUserInfo && preparedUserInfo.loginEmpName),
                    has_site_levels: Boolean(siteLevelsResult && siteLevelsResult.ok),
                    site_levels_error: (siteLevelsResult && siteLevelsResult.error) || ''
                };
            }
            """,
            self._load_page_user_info(),
        )
        if not isinstance(result, dict) or not result.get("has_user_info"):
            raise RuntimeError("browser resolver user info missing")
        if not result.get("has_site_levels"):
            raise RuntimeError(str(result.get("site_levels_error") or "").strip() or "SITE_LEVELS selection missing")

    def _clear_resolution_fields(self) -> None:
        if self._frame is None:
            raise RuntimeError("browser resolver frame not ready")
        self._frame.evaluate(
            """
            () => {
                const controlIds = [
                    'ACCEPT_PROVINCE',
                    'ACCEPT_CITY',
                    'ACCEPT_COUNTY',
                    'ACCEPT_TOWN',
                    'DESTINATION_CODE',
                    'DESTINATION_CENTER_CODE',
                    'DISPATCH_UNDERLING_SITE_CODE',
                ];
                for (const id of controlIds) {
                    const control = window.mini && mini.get(id);
                    if (control && control.setValue) control.setValue('');
                    if (control && control.setText) control.setText('');
                }
                const inputIds = [
                    'ACCEPT_PROVINCE$text',
                    'ACCEPT_CITY$text',
                    'ACCEPT_COUNTY$text',
                    'ACCEPT_COUNTY$value',
                    'ACCEPT_TOWN$text',
                    'DESTINATION_CODE$text',
                    'DESTINATION_CODE$value',
                    'DESTINATION_CENTER_CODE$text',
                    'DESTINATION_CENTER_CODE$value',
                    'DISPATCH_UNDERLING_SITE_CODE$text',
                    'DISPATCH_UNDERLING_SITE_CODE$value',
                ];
                for (const id of inputIds) {
                    const el = document.getElementById(id);
                    if (el) el.value = '';
                }
            }
            """
        )

    def _fire_address_blur(self, address: str) -> None:
        if self._frame is None:
            raise RuntimeError("browser resolver frame not ready")
        self._frame.evaluate(
            """
            (target) => {
                const addressControl = window.mini && mini.get('ACCEPT_MAN_ADDRESS');
                if (!addressControl || !addressControl.setValue || !addressControl.fire) {
                    throw new Error('ACCEPT_MAN_ADDRESS mini control missing');
                }
                addressControl.setValue(target);
                addressControl.fire('valuechanged', {value: target, newValue: target});
                addressControl.fire('blur', {value: target});
            }
            """,
            address,
        )

    def _resolve_once(self, address: str) -> Dict[str, str]:
        if self._page is None or self._frame is None:
            raise RuntimeError("browser resolver frame not ready")

        self._wait_page_ready()
        self._prepare_entry_page_context()
        self._clear_resolution_fields()
        self._fire_address_blur(address)

        self._frame.wait_for_function(
            """
            (target) => {
                const gv = (id) => (document.getElementById(id) || {}).value || '';
                const destCode = gv('DESTINATION_CODE$value');
                const dispatchCode = gv('DISPATCH_UNDERLING_SITE_CODE$value');
                const currentAddress = gv('ACCEPT_MAN_ADDRESS$text');
                if (currentAddress !== target) {
                    return false;
                }
                return Boolean(destCode || dispatchCode);
            }
            """,
            arg=address,
            timeout=30000,
        )
        values = self._read_values()
        if not values["destination_code"]:
            raise RuntimeError("browser resolver destination code remained empty")
        return values

    def _read_values(self) -> Dict[str, str]:
        if self._frame is None:
            raise RuntimeError("browser resolver frame not ready")
        raw = self._frame.evaluate(
            """
            () => {
                const gv = (id) => (document.getElementById(id) || {}).value || '';
                return {
                    address: gv('ACCEPT_MAN_ADDRESS$text'),
                    province: gv('ACCEPT_PROVINCE$text'),
                    city: gv('ACCEPT_CITY$text'),
                    county_text: gv('ACCEPT_COUNTY$text'),
                    county_value: gv('ACCEPT_COUNTY$value'),
                    town: gv('ACCEPT_TOWN$text'),
                    destination_name: gv('DESTINATION_CODE$text'),
                    destination_code: gv('DESTINATION_CODE$value'),
                    destination_center_name: gv('DESTINATION_CENTER_CODE$text'),
                    destination_center_code: gv('DESTINATION_CENTER_CODE$value'),
                    dispatch_site_name: gv('DISPATCH_UNDERLING_SITE_CODE$text'),
                    dispatch_site_code: gv('DISPATCH_UNDERLING_SITE_CODE$value'),
                };
            }
            """
        )
        county_text = str(raw.get("county_text", "") or raw.get("county_value", "") or "")
        county_parts = [part.strip() for part in county_text.split("|") if part.strip()]
        province = str(raw.get("province", "") or (county_parts[0] if county_parts else ""))
        city = str(raw.get("city", "") or (county_parts[1] if len(county_parts) > 1 else ""))
        county = str(raw.get("county_value", "") or (county_parts[-1] if county_parts else ""))
        if "|" in county:
            county = county_parts[-1] if county_parts else ""
        return {
            "address": str(raw.get("address", "") or ""),
            "province": province,
            "city": city,
            "county": county,
            "town": str(raw.get("town", "") or ""),
            "destination_name": str(raw.get("destination_name", "") or ""),
            "destination_code": str(raw.get("destination_code", "") or ""),
            "destination_center_name": str(raw.get("destination_center_name", "") or ""),
            "destination_center_code": str(raw.get("destination_center_code", "") or ""),
            "dispatch_site_name": str(raw.get("dispatch_site_name", "") or ""),
            "dispatch_site_code": str(raw.get("dispatch_site_code", "") or ""),
        }
