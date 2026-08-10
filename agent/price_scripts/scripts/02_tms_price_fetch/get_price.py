# -*- coding: utf-8 -*-
"""
Compute product prices and dispatch-site info for a given address/weight/volume.

Flow (derived from the TMS front-end logic):
1) /map/inputtipsDTH -> parse address + area name
2) /dataQuery/findAllByCallId?id=FIND_DESTINATION_BY_NAME -> destination list
3) /dataQuery/findAllByCallId?id=FIND_CREATE_BILL_DESTINATION -> destination detail
4) /dataQuery/findAllByCallId?id=FIND_TAB_WEIGHT_RATIO -> volume weight ratio
5) /dataQuery/findAllByCallId?id=FIND_CREATE_BILL_SEND_CENTER -> send center
6) /dataQuery/findAllByCallId?id=FIND_CREATE_BILL_DISP_CENTER(_NEW) -> destination center
7) /dataQuery/findAllByCallId?id=FIND_PLAN_GOODS_ROUTE -> plan route name
8) /dataOperation/batchExecProcedure (P_CALC_CLIENT_PRICE_BILL_SH_ZB) -> fee breakdown
9) /dataQuery/findAllByCallId?id=FIND_SITE_DISP_INFO&SITE_CODE=... -> site info

Notes:
- Skip empty or "0" site-info fields.
- If a product is not in the product list for the weight band, its price is omitted.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import re
import threading
import sys
from urllib.parse import unquote
from typing import Any, Dict, Iterable, List, Optional, Tuple

from dotenv import load_dotenv
import os

SCRIPT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SCRIPT_ROOT)

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
load_dotenv(os.path.join(PROJECT_ROOT, ".env"))

from login_manager import TMSAuth
from shared.address_utils import (
    generate_address_candidates,
    generate_refined_address_candidates,
    is_service_hall_name,
)
from shared.price_utils import calc_sum_fee

BASE_ORIGIN = "https://tms.ronghuiwl.com"
MAP_INPUTTIPS_URL = f"{BASE_ORIGIN}/map/inputtipsDTH"
DATA_QUERY_URL = f"{BASE_ORIGIN}/dataQuery/findAllByCallId"
BATCH_PROC_URL = f"{BASE_ORIGIN}/dataOperation/batchExecProcedure"
EXEC_PROC_URL = f"{BASE_ORIGIN}/dataOperation/execProcedure"
INDEX_URL = f"{BASE_ORIGIN}/module/index?mv=index"

PROC_CALC_PRICE = "P_CALC_CLIENT_PRICE_BILL_SH_ZB"

DEFAULT_HEADERS = {
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    "Origin": BASE_ORIGIN,
    "Referer": f"{BASE_ORIGIN}/widget/home",
    "X-Requested-With": "XMLHttpRequest",
}

DISCOUNT_KEYS = {
    "TRANSPORT_FEE_DIS",
    "OPERATE_FEE_DIS",
    "REC_DISPATCH_FEE_DIS",
    "TRANSFER_FEE_DIS",
    "REC_SHORTHAUL_FEE_DIS",
    "REC_PERIOD_FEE_DIS",
}

PAGE_PRICING_CONTEXT_FIELDS = ("BL_INSURESTATUS",)

ENTRY_DEFAULT_INSURANCE = "3000"
ENTRY_DEFAULT_INSURANCE_FEE = "3"

PRODUCTS = [
    {"PRODUCT_CODE": "001", "PRODUCT_TYPE": "融惠达"},
    {"PRODUCT_CODE": "002", "PRODUCT_TYPE": "精准零担"},
    {"PRODUCT_CODE": "003", "PRODUCT_TYPE": "融安达"},
    {"PRODUCT_CODE": "004", "PRODUCT_TYPE": "融速达"},
]

DEFAULT_LOGIN_USER = os.getenv("TMS_DAXIANGUSERNAME")
DEFAULT_LOGIN_PASS = os.getenv("TMS_DAXIANGPASSWORD")

_BROWSER_RESOLVER = None
_BROWSER_RESOLVER_LOCK = threading.Lock()


class PriceCalcError(RuntimeError):
    pass


def _get_browser_resolver():
    global _BROWSER_RESOLVER
    if _BROWSER_RESOLVER is not None:
        return _BROWSER_RESOLVER
    with _BROWSER_RESOLVER_LOCK:
        if _BROWSER_RESOLVER is None:
            from browser_address_resolver import BrowserAddressResolver

            _BROWSER_RESOLVER = BrowserAddressResolver(
                username=DEFAULT_LOGIN_USER or "",
                password=DEFAULT_LOGIN_PASS or "",
            )
    return _BROWSER_RESOLVER


def _extract_input_value(html: str, input_id: str) -> Optional[str]:
    pattern = rf"id=[\"']{re.escape(input_id)}[\"'][^>]*value=[\"']([^\"']*)[\"']"
    match = re.search(pattern, html)
    if not match:
        return None
    return match.group(1)


def _js_unescape(value: str) -> str:
    if not value:
        return ""

    def _replace_unicode(match: re.Match[str]) -> str:
        return chr(int(match.group(1), 16))

    text = re.sub(r"%u([0-9A-Fa-f]{4})", _replace_unicode, value)
    return unquote(text)


def _clean_str(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _skip_value(value: Any) -> bool:
    if value is None:
        return True
    text = str(value).strip()
    if not text:
        return True
    return text in {"0", "0.0", "0.00"}


def _format_money(value: Optional[float]) -> Optional[str]:
    if value is None:
        return None
    s = f"{value:.2f}"
    if s.endswith(".00"):
        s = s[:-3]
    elif s.endswith("0"):
        s = s[:-1]
    return f"{s}元"


def _pick_first_center(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    if not rows:
        return {}
    filtered = [row for row in rows if str(row.get("SITE_CODE", "")) != "571359"]
    return filtered[0] if filtered else rows[0]


def _center_code_name(row: Dict[str, Any]) -> Tuple[str, str]:
    code = _clean_str(
        row.get("DESTINATION_CENTER_CODE")
        or row.get("SITE_CODE")
        or row.get("CENTER_CODE")
    )
    name = _clean_str(
        row.get("DESTINATION_CENTER")
        or row.get("SITE_NAME")
        or row.get("CENTER_NAME")
    )
    return code, name


def _post_json_list(session, call_id: str, payload: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    resp = session.post(
        DATA_QUERY_URL,
        params={"id": call_id},
        data=payload or {},
        headers=DEFAULT_HEADERS,
        timeout=20,
    )
    resp.raise_for_status()
    data = resp.json()
    if isinstance(data, dict) and "data" in data:
        data = data.get("data")
    if not isinstance(data, list):
        return []
    return [item for item in data if isinstance(item, dict)]


def _exec_procedure(session, procedure_name: str, rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    resp = session.post(
        EXEC_PROC_URL,
        data={
            "procedureName": procedure_name,
            "data": json.dumps(rows, ensure_ascii=False),
        },
        headers=DEFAULT_HEADERS,
        timeout=20,
    )
    resp.raise_for_status()
    data = resp.json()
    return data if isinstance(data, dict) else {}


def _read_user_info_cookie(session) -> Dict[str, Any]:
    raw = session.cookies.get("userInfo")
    if not raw:
        return {}
    try:
        decoded = _js_unescape(raw)
        data = json.loads(decoded)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _fetch_login_context(session) -> Dict[str, str]:
    resp = session.get(INDEX_URL, timeout=15)
    resp.raise_for_status()
    html = resp.text

    site_code = _extract_input_value(html, "loginSiteCode") or ""
    site_name = _extract_input_value(html, "loginSiteName") or ""
    user_info = _read_user_info_cookie(session)
    emp_code = _clean_str(user_info.get("loginEmpCode", ""))
    emp_name = _clean_str(user_info.get("loginEmpName", ""))

    return {
        "site_code": site_code,
        "site_name": site_name,
        "emp_code": emp_code,
        "emp_name": emp_name,
    }


def _fetch_site_and_center(session, site_code: str) -> Dict[str, Any]:
    if not site_code:
        return {}
    rows = _post_json_list(session, "FIND_SITE_AND_CENTER", {"SITE_CODE": site_code})
    return rows[0] if rows else {}


def _fetch_send_center(session, send_site_code: str) -> Dict[str, Any]:
    if not send_site_code:
        return {}
    rows = _post_json_list(session, "FIND_CREATE_BILL_SEND_CENTER", {"SEND_SITE_CODE": send_site_code})
    return _pick_first_center(rows)


def _fetch_destination_center(session, dispatch_site_code: str, send_center_code: str) -> Dict[str, Any]:
    if not dispatch_site_code:
        return {}
    payload = {"DISPATCH_SITE_CODE": dispatch_site_code}
    if send_center_code:
        payload["SEND_SITE_CENTER_CODE"] = send_center_code
    rows = _post_json_list(session, "FIND_CREATE_BILL_DISP_CENTER_NEW", payload)
    if not rows:
        rows = _post_json_list(session, "FIND_CREATE_BILL_DISP_CENTER", {"DISPATCH_SITE_CODE": dispatch_site_code})
    return _pick_first_center(rows)


def _fetch_plan_route_name(session, send_center_code: str, destination_center_code: str) -> str:
    if not send_center_code or not destination_center_code:
        return ""
    rows = _post_json_list(
        session,
        "FIND_PLAN_GOODS_ROUTE",
        {
            "DESTINATION_CENTER_CODE": destination_center_code,
            "SEND_CENTER_CODE": send_center_code,
        },
    )
    if rows:
        return _clean_str(rows[0].get("ROUTE_NAME"))
    return ""


def _pick_best_area_name(session, area_results: List[Dict[str, Any]]) -> str:
    names: List[str] = []
    for item in area_results:
        text = _clean_str(item.get("areaName"))
        if text and text not in names:
            names.append(text)
    if not names:
        raise PriceCalcError("address parse missing areaName")
    if len(names) == 1:
        return names[0]

    try:
        proc_result = _exec_procedure(
            session,
            "GET_FIRST_MATCH_SITE",
            [{"p_site_names": ", ".join(names)}],
        ).get("result") or {}
    except Exception:
        proc_result = {}

    match_name = _clean_str(proc_result.get("p_match_name", ""))
    if match_name and match_name in names:
        return match_name

    match_index = proc_result.get("p_match_index")
    if match_index not in (None, ""):
        try:
            idx = int(float(match_index)) - 1
            if 0 <= idx < len(names):
                return names[idx]
        except Exception:
            pass

    non_service = [name for name in names if not is_service_hall_name(name)]
    return non_service[0] if non_service else names[0]


def _parse_address(session, address: str, ctx: Dict[str, str], emp_code: str, emp_name: str) -> Dict[str, str]:
    payload = {
        "address": address,
        "createMan": emp_name,
        "createManCode": emp_code,
        "createSite": ctx.get("site_name", ""),
        "createSiteCode": ctx.get("site_code", ""),
    }
    resp = session.post(MAP_INPUTTIPS_URL, data=payload, headers=DEFAULT_HEADERS, timeout=20)
    resp.raise_for_status()
    data = resp.json()

    if not isinstance(data, dict) or not data.get("success"):
        raise PriceCalcError(f"address parse failed: {data}")

    result_list = data.get("data", {}).get("result") or []
    if not result_list:
        raise PriceCalcError("address parse returned empty result")

    result0 = result_list[0]
    geo = result0.get("geoResult") or {}
    area_results = result0.get("areaResults") or []
    area_names = [_clean_str(item.get("areaName")) for item in area_results if _clean_str(item.get("areaName"))]
    if not area_names:
        raise PriceCalcError("address parse missing areaName")

    area_name = _pick_best_area_name(session, area_results)
    return {
        "area_name": area_name,
        "area_names": area_names,
        "province": _clean_str(geo.get("province")),
        "city": _clean_str(geo.get("city")),
        "county": _clean_str(geo.get("county")),
        "town": _clean_str(geo.get("town")),
    }


def _build_destination_search_names(
    addr_info: Dict[str, str],
    *,
    city_hint: str = "",
    district_hint: str = "",
) -> List[str]:
    def _expand_name_aliases(name: str) -> List[str]:
        text = _clean_str(name)
        if not text:
            return []

        aliases = [text]
        normalized = text
        for pattern in (
            r"\d+优化中$",
            r"优化中$",
            r"\d+优化$",
            r"优化$",
        ):
            normalized = re.sub(pattern, "", normalized)
        normalized = _clean_str(normalized)
        if normalized and normalized not in aliases:
            aliases.append(normalized)
        return aliases

    district = _clean_str(district_hint or addr_info.get("county", ""))
    city = _clean_str(city_hint or addr_info.get("city", ""))

    names: List[str] = []
    for name in (
        district,
        f"{city}{district}" if city and district else "",
        city,
    ):
        text = _clean_str(name)
        if text and text not in names:
            names.append(text)

    for name in (
        addr_info.get("area_name", ""),
        *(addr_info.get("area_names", []) or []),
    ):
        for alias in _expand_name_aliases(name):
            if alias not in names:
                names.append(alias)
    return names


def _resolve_destination_from_names(
    session,
    search_names: List[str],
    addr_info: Dict[str, str],
) -> Dict[str, Any]:
    last_error = "destination detail missing"

    for search_name in search_names:
        dest_candidates = _post_json_list(
            session,
            "FIND_DESTINATION_BY_NAME",
            {"DESTINATION_NAME": search_name},
        )
        if not dest_candidates:
            last_error = f"destination candidates missing: {search_name}"
            continue

        try:
            best_dest = _filter_best_destination(dest_candidates, addr_info, search_name=search_name)
            temp_dest_code = _resolve_destination_code(best_dest)
        except PriceCalcError as exc:
            last_error = f"destination unavailable: {exc}"
            continue

        dest_list = _post_json_list(
            session,
            "FIND_CREATE_BILL_DESTINATION",
            {"DESTINATION_CODE": temp_dest_code},
        )
        if not dest_list:
            fallback_destination = _build_destination_from_site_candidate(session, temp_dest_code, best_dest)
            if fallback_destination:
                dispatch = _compute_dispatch_info(fallback_destination)
                if dispatch.get("dispatch_site_code"):
                    return {
                        "search_name": search_name,
                        "destination": fallback_destination,
                        "dispatch": dispatch,
                        "temp_dest_code": temp_dest_code,
                    }
            last_error = f"destination detail missing: {search_name}"
            continue

        destination = dest_list[0]
        dispatch = _compute_dispatch_info(destination)
        if dispatch.get("dispatch_site_code"):
            return {
                "search_name": search_name,
                "destination": destination,
                "dispatch": dispatch,
                "temp_dest_code": temp_dest_code,
            }

        last_error = f"dispatch site missing: {search_name}"

    raise PriceCalcError(last_error)


def _build_destination_from_site_candidate(
    session,
    temp_dest_code: str,
    best_dest: Dict[str, Any],
) -> Dict[str, Any]:
    if _clean_str(best_dest.get("TYPE")) != "一级网点":
        return {}

    site_code = _clean_str(best_dest.get("DESTINATION_CODE")) or temp_dest_code
    if not site_code:
        return {}

    site_name = _clean_str(best_dest.get("DESTINATION_NAME")) or _clean_str(best_dest.get("SITE_NAME"))
    try:
        site_info = _fetch_site_and_center(session, site_code)
    except Exception:
        site_info = {}

    destination = {
        "DESTINATION_CODE": site_code,
        "DESTINATION_NAME": site_name or _clean_str(site_info.get("SITE_NAME", "")),
        "SITE_CODE": _clean_str(site_info.get("SITE_CODE", "")) or site_code,
        "SITE_NAME": _clean_str(site_info.get("SITE_NAME", "")) or site_name,
        "SITE_TYPE": _clean_str(site_info.get("TYPE", "")) or _clean_str(best_dest.get("TYPE", "")),
        "SUPERIOR_SITE_CODE": _clean_str(site_info.get("SUPERIOR_SITE_CODE", "")),
        "SUPERIOR_SITE": _clean_str(site_info.get("SUPERIOR_SITE", "")),
        "SUPERIOR_FINANCE_CENTER_CODE": _clean_str(site_info.get("SUPERIOR_FINANCE_CENTER_CODE", "")),
        "SUPERIOR_FINANCE_CENTER": _clean_str(site_info.get("SUPERIOR_FINANCE_CENTER", "")),
        "FINANCE_CENTER_CODE": _clean_str(site_info.get("FINANCE_CENTER_CODE", "")),
        "FINANCE_CENTER": _clean_str(site_info.get("FINANCE_CENTER", "")),
        "ENABLE_ACCOUNT_PURPOSE": _clean_str(best_dest.get("ENABLE_ACCOUNT_PURPOSE", "")),
        "BL_PURPOSE_SITE": _clean_str(best_dest.get("BL_PURPOSE_SITE", "")),
        "BL_OPEN": _clean_str(best_dest.get("BL_OPEN", "")),
    }
    if not destination["SITE_NAME"]:
        return {}
    return destination


def _normalize_region_token(text: str) -> str:
    value = _clean_str(text)
    for suffix in (
        "特别行政区",
        "壮族自治区",
        "回族自治区",
        "维吾尔自治区",
        "自治区",
        "自治州",
        "地区",
        "盟",
        "省",
        "市",
        "区",
        "县",
        "旗",
    ):
        if value.endswith(suffix):
            value = value[: -len(suffix)]
            break
    return value


def _region_match_score(
    addr_info: Dict[str, str],
    *,
    province_hint: str = "",
    city_hint: str = "",
    district_hint: str = "",
) -> Tuple[int, int, int]:
    resolved_province = _normalize_region_token(addr_info.get("province", ""))
    resolved_city = _normalize_region_token(addr_info.get("city", ""))
    resolved_district = _normalize_region_token(addr_info.get("county", ""))
    expected_province = _normalize_region_token(province_hint)
    expected_city = _normalize_region_token(city_hint)
    expected_district = _normalize_region_token(district_hint)

    return (
        1 if expected_province and resolved_province == expected_province else 0,
        1 if expected_city and resolved_city == expected_city else 0,
        1 if expected_district and resolved_district == expected_district else 0,
    )


def _candidate_resolution_score(
    candidate_address: str,
    original_address: str,
    dispatch_site_name: str,
    addr_info: Dict[str, str],
    *,
    province_hint: str = "",
    city_hint: str = "",
    district_hint: str = "",
) -> Tuple[int, int, int, int, int]:
    return (
        1 if not is_service_hall_name(dispatch_site_name) else 0,
        *_region_match_score(
            addr_info,
            province_hint=province_hint,
            city_hint=city_hint,
            district_hint=district_hint,
        ),
        1 if candidate_address == original_address else 0,
        -len(candidate_address),
    )


def _resolve_destination_via_browser(
    session,
    address: str,
) -> Dict[str, Any]:
    try:
        browser_result = _get_browser_resolver().resolve(address)
    except Exception as exc:
        raise PriceCalcError(str(exc))

    destination_code = _clean_str(browser_result.get("destination_code", ""))
    destination_name = _clean_str(browser_result.get("destination_name", ""))

    addr_info = {
        "area_name": destination_name or _clean_str(browser_result.get("dispatch_site_name", "")),
        "area_names": [
            value
            for value in (
                destination_name,
                _clean_str(browser_result.get("dispatch_site_name", "")),
            )
            if value
        ],
        "province": _clean_str(browser_result.get("province", "")),
        "city": _clean_str(browser_result.get("city", "")),
        "county": _clean_str(browser_result.get("county", "")),
        "town": _clean_str(browser_result.get("town", "")),
    }

    if not destination_code:
        raise PriceCalcError("browser resolver destination code missing")
    pricing_context = browser_result.get("pricing_context")
    if not isinstance(pricing_context, dict):
        raise PriceCalcError("browser resolver pricing context missing")
    missing_pricing_fields = [
        field for field in PAGE_PRICING_CONTEXT_FIELDS if field not in pricing_context
    ]
    if missing_pricing_fields:
        raise PriceCalcError(
            "browser resolver pricing context missing: " + ", ".join(missing_pricing_fields)
        )

    dest_list = _post_json_list(
        session,
        "FIND_CREATE_BILL_DESTINATION",
        {"DESTINATION_CODE": destination_code},
    )
    if not dest_list:
        raise PriceCalcError(f"destination detail missing: {destination_name or destination_code}")

    destination = dest_list[0]
    dispatch = _compute_dispatch_info(destination)
    if not dispatch.get("dispatch_site_code") and browser_result.get("dispatch_site_code"):
        dispatch["dispatch_site_code"] = _clean_str(browser_result.get("dispatch_site_code", ""))
        dispatch["dispatch_site_name"] = _clean_str(browser_result.get("dispatch_site_name", ""))
    if not dispatch.get("dispatch_site_code"):
        raise PriceCalcError("browser resolver dispatch site missing")

    return {
        "used_address": _clean_str(browser_result.get("address", "")) or _clean_str(address),
        "addr_info": addr_info,
        "search_name": destination_name,
        "destination": destination,
        "dispatch": dispatch,
        "temp_dest_code": destination_code,
        "pricing_context": {
            field: pricing_context[field] for field in PAGE_PRICING_CONTEXT_FIELDS
        },
    }


def resolve_address_destination(
    session,
    address: str,
    ctx: Dict[str, str],
    emp_code: str,
    emp_name: str,
    *,
    province_hint: str = "",
    city_hint: str = "",
    district_hint: str = "",
    candidate_addresses: Optional[Iterable[str]] = None,
    allow_refined_on_service_hall: bool = True,
) -> Dict[str, Any]:
    _ = (
        ctx,
        emp_code,
        emp_name,
        province_hint,
        city_hint,
        district_hint,
        candidate_addresses,
        allow_refined_on_service_hall,
    )
    original_address = _clean_str(address)
    if not original_address:
        raise PriceCalcError("address is empty")
    return _resolve_destination_via_browser(session, original_address)


def _filter_best_destination(
    dest_candidates: List[Dict[str, Any]],
    addr_info: Dict[str, str],
    *,
    search_name: str = "",
) -> Dict[str, Any]:
    """
    从 FIND_DESTINATION_BY_NAME 返回的多个候选中，选出与地址省市匹配的候选。

    当同名区县存在于多个城市时（如"龙华区"在海口/深圳），API 返回多个候选，
    需要用 _parse_address 解析出的省市信息进行过滤。

    策略（瀑布式）：
    1. 候选字段中包含城市名 → 精确匹配
    2. 候选字段中包含省份名 → 省级匹配
    3. 回退到第一个候选（保持单候选场景的原有行为）
    """
    province = _clean_str(addr_info.get("province", ""))
    city = _clean_str(addr_info.get("city", ""))
    county = _clean_str(addr_info.get("county", ""))

    def _all_str_values(d: Dict[str, Any]) -> str:
        return "".join(str(v) for v in d.values() if isinstance(v, str))

    def _region_tokens(*values: str) -> List[str]:
        tokens: List[str] = []
        for value in values:
            text = _clean_str(value)
            normalized = _normalize_region_token(text)
            for token in (text, normalized):
                if token and token not in tokens:
                    tokens.append(token)
        return tokens

    city_tokens = _region_tokens(city)
    province_tokens = _region_tokens(province)
    county_tokens = _region_tokens(county)
    expected_tokens = county_tokens + city_tokens + province_tokens

    def _contains_any(text: str, tokens: List[str]) -> bool:
        return any(token and token in text for token in tokens)

    def _candidate_matches_region(candidate: Dict[str, Any]) -> bool:
        text = _all_str_values(candidate)
        return _contains_any(text, expected_tokens)

    def _search_name_matches_region() -> bool:
        text = _clean_str(search_name)
        return _contains_any(text, expected_tokens)

    def _candidate_has_explicit_region_context(candidate: Dict[str, Any]) -> bool:
        key_markers = (
            "PROVINCE",
            "CITY",
            "COUNTY",
            "DISTRICT",
            "AREA",
            "ADDRESS",
            "ADDR",
            "省",
            "市",
            "区",
            "县",
            "地址",
        )
        value_markers = ("省", "市", "自治区", "特别行政区")
        for key, value in candidate.items():
            if not isinstance(value, str) or not value.strip():
                continue
            key_text = str(key).upper()
            if any(marker in key_text for marker in key_markers):
                return True
            if any(marker in value for marker in value_markers):
                return True
        return False

    if expected_tokens:
        region_matches = [candidate for candidate in dest_candidates if _candidate_matches_region(candidate)]
        if region_matches:
            dest_candidates = region_matches
        elif not _search_name_matches_region() and (
            len(dest_candidates) == 1
            or any(_candidate_has_explicit_region_context(candidate) for candidate in dest_candidates)
        ):
            raise PriceCalcError(f"destination region mismatch: {search_name or 'unknown'}")

    if len(dest_candidates) <= 1:
        return dest_candidates[0] if dest_candidates else {}

    city_short = city_tokens[-1] if city_tokens else ""
    prov_short = province_tokens[-1] if province_tokens else ""

    # 策略 1：城市名匹配
    if city_short:
        matches = [c for c in dest_candidates if city_short in _all_str_values(c)]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1 and county:
            county_matches = [c for c in matches if county in _all_str_values(c)]
            if county_matches:
                return county_matches[0]
            return matches[0]
        if matches:
            return matches[0]

    # 策略 2：省份匹配
    if prov_short:
        matches = [c for c in dest_candidates if prov_short in _all_str_values(c)]
        if matches:
            return matches[0]

    # 策略 3：回退到第一个
    return dest_candidates[0]


def _resolve_destination_code(dest0: Dict[str, Any]) -> str:
    temp_code = _clean_str(dest0.get("DESTINATION_CODE"))
    if not temp_code:
        raise PriceCalcError("destination code missing")

    if _clean_str(dest0.get("TYPE")) == "二级网点":
        enable = str(dest0.get("ENABLE_ACCOUNT_PURPOSE", ""))
        bl_purpose = str(dest0.get("BL_PURPOSE_SITE", ""))
        if enable == "1" and bl_purpose == "0":
            temp_code = _clean_str(dest0.get("SUPERIOR_SITE_CODE")) or temp_code
        elif enable == "0" and bl_purpose == "1":
            raise PriceCalcError("destination paused for delivery")
        elif enable == "0" and bl_purpose == "0":
            raise PriceCalcError("destination not available")

    return temp_code


def _compute_dispatch_info(dest: Dict[str, Any]) -> Dict[str, str]:
    dispatch_site_code = _clean_str(dest.get("SITE_CODE"))
    dispatch_site_name = _clean_str(dest.get("SITE_NAME"))

    if _clean_str(dest.get("SITE_TYPE")) == "二级网点":
        enable = str(dest.get("ENABLE_ACCOUNT_PURPOSE", ""))
        bl_purpose = str(dest.get("BL_PURPOSE_SITE", ""))
        bl_open = str(dest.get("BL_OPEN", ""))
        if enable == "1" and (bl_purpose == "1" or bl_open == "1"):
            dispatch_site_code = _clean_str(dest.get("SUPERIOR_SITE_CODE")) or dispatch_site_code
            dispatch_site_name = _clean_str(dest.get("SUPERIOR_SITE")) or dispatch_site_name

    return {
        "dispatch_site_code": dispatch_site_code,
        "dispatch_site_name": dispatch_site_name,
        "dispatch_finance_center": _clean_str(dest.get("FINANCE_CENTER")) or _clean_str(dest.get("SUPERIOR_FINANCE_CENTER")),
        "dispatch_finance_center_code": _clean_str(dest.get("FINANCE_CENTER_CODE")) or _clean_str(dest.get("SUPERIOR_FINANCE_CENTER_CODE")),
    }


def _fetch_weight_ratio(session, send_site_code: str, destination_code: str) -> int:
    payload = {
        "SEND_SITE_CODE": send_site_code,
        "DESTINATION_CODE": destination_code,
    }
    rows = _post_json_list(session, "FIND_TAB_WEIGHT_RATIO", payload)
    if rows and rows[0].get("WEIGHT_RATIO"):
        try:
            return int(float(rows[0]["WEIGHT_RATIO"]))
        except Exception:
            pass
    return 5000


def _volume_weight(volume: float, weight_ratio: int) -> float:
    return float(volume) * 1_000_000 / float(weight_ratio)


def _settlement_weight(bill_weight: float, volume_weight: float) -> float:
    return max(float(bill_weight), float(volume_weight))


def _available_products(settlement_weight: float, use_collar: bool = False) -> List[Dict[str, str]]:
    if use_collar:
        return [p for p in PRODUCTS if p["PRODUCT_CODE"] == "003"]
    if 0 < settlement_weight <= 800:
        return [p for p in PRODUCTS if p["PRODUCT_CODE"] in {"003", "004"}]
    if 800 < settlement_weight <= 3000:
        return [p for p in PRODUCTS if p["PRODUCT_CODE"] in {"002", "003", "004"}]
    return [p for p in PRODUCTS if p["PRODUCT_CODE"] in {"001", "002", "003", "004"}]


def _calc_sum_fee(calc_data: Dict[str, Any]):
    return calc_sum_fee(calc_data, DISCOUNT_KEYS)


def _calc_price(session, data: Dict[str, Any]):
    payload = {
        "procedureName": PROC_CALC_PRICE,
        "data": json.dumps([data], ensure_ascii=False),
    }
    resp = session.post(BATCH_PROC_URL, data=payload, headers=DEFAULT_HEADERS, timeout=30)
    resp.raise_for_status()
    result = resp.json()
    calc_data = (
        result.get("result", {}).get("data") if isinstance(result, dict) else None
    )
    if not calc_data or not isinstance(calc_data, list):
        return None
    row = calc_data[0]
    if not isinstance(row, dict):
        return None
    return _calc_sum_fee(row)


def _fetch_site_info(session, site_code: str) -> Dict[str, Any]:
    resp = session.post(
        DATA_QUERY_URL,
        params={"id": "FIND_SITE_DISP_INFO", "SITE_CODE": site_code},
        data={},
        headers=DEFAULT_HEADERS,
        timeout=20,
    )
    resp.raise_for_status()
    data = resp.json()
    if isinstance(data, dict) and "data" in data:
        data = data.get("data")
    if not isinstance(data, list) or not data:
        return {}
    if not isinstance(data[0], dict):
        return {}
    return data[0]


def _build_base_payload(
    ctx: Dict[str, str],
    addr_info: Dict[str, str],
    destination: Dict[str, Any],
    dispatch: Dict[str, str],
    address: str,
    weight: float,
    volume: float,
    volume_weight: float,
    settlement_weight: float,
    emp_code: str,
    emp_name: str,
) -> Dict[str, Any]:
    now = dt.datetime.now()
    send_date = now.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    use_date = now.strftime("%Y-%m-%d %H:%M:%S")

    province = addr_info.get("province", "")
    city = addr_info.get("city", "")
    county = addr_info.get("county", "")
    town = addr_info.get("town", "")
    accept_county = county

    ton_square = settlement_weight / 100.0 if settlement_weight else 0.0

    return {
        "SEND_DATE": send_date,
        "USE_DATE": use_date,
        "SEND_SITE_CODE": ctx.get("site_code", ""),
        "SEND_SITE": ctx.get("site_name", ""),
        "REGISTER_SITE_CODE": ctx.get("site_code", ""),
        "REGISTER_SITE": ctx.get("site_name", ""),
        "TAKE_PIECE_EMPLOYEE_CODE": emp_code,
        "TAKE_PIECE_EMPLOYEE": emp_name,
        "DISPATCH_UNDERLING_SITE_CODE": dispatch.get("dispatch_site_code", ""),
        "DISPATCH_UNDERLING_SITE": dispatch.get("dispatch_site_name", ""),
        "DISPATCH_SITE_CODE": dispatch.get("dispatch_site_code", ""),
        "DISPATCH_SITE": dispatch.get("dispatch_site_name", ""),
        "DISPATCH_FINANCE_CENTER": dispatch.get("dispatch_finance_center", ""),
        "DISPATCH_FINANCE_CENTER_CODE": dispatch.get("dispatch_finance_center_code", ""),
        "DESTINATION_CODE": _clean_str(destination.get("DESTINATION_CODE")),
        "DESTINATION": _clean_str(destination.get("DESTINATION_NAME")) or _clean_str(destination.get("SITE_NAME")),
        "DESTINATION_CENTER_CODE": _clean_str(destination.get("DESTINATION_CENTER_CODE")),
        "DESTINATION_CENTER": _clean_str(destination.get("DESTINATION_CENTER")),
        "ACCEPT_PROVINCE": province,
        "ACCEPT_CITY": city,
        "ACCEPT_COUNTY": accept_county,
        "ACCEPT_TOWN": town,
        "ACCEPT_MAN_ADDRESS": address,
        "BILL_WEIGHT": f"{weight:.2f}",
        "FEE_WEIGHT": f"{settlement_weight:.2f}",
        "SETTLEMENT_WEIGHT": f"{settlement_weight:.2f}",
        "VOLUME": f"{volume}",
        "VOLUME_WEIGHT": f"{volume_weight:.2f}",
        "PAYLOAD_WEIGHT": f"{volume_weight:.2f}",
        "WEIGHT": f"{settlement_weight:.2f}",
        "TON_SQUARE": f"{ton_square:.2f}",
        "PIECE_NUMBER": "1",
        "CLASS_TYPE": "汽运",
        "SOON_PIECE_TYPE": "汽运",
        "CLASS": "汽运",
        "DISPATCH_MODE": "自提",
        "GOODS_TYPE": "重货",
        "BL_ELECTRON": "1",
        "BL_MESSAGE": "1",
        "BL_ACCEPT_MESSAGE": "1",
        "BL_GIS": "1",
        "COLLECTING_IS_NOT": "0",
        "ONLINE_PAY": "0",
        "REBATE_CYCLE": "T+3",
        "PAYMENT_TYPE": "现金",
        "TOPAYMENT": "0",
        "INSURANCE": ENTRY_DEFAULT_INSURANCE,
        "INSURANCE_FEE": ENTRY_DEFAULT_INSURANCE_FEE,
        "TRANSPORT_FEE": "0",
        "OPERATE_FEE": "0",
        "TRANSFER_FEE": "0",
        "REC_DISPATCH_FEE": "0",
        "PERIOD_FEE": "0",
        "OTHER_FEE1": "0",
        "OTHER_FEE2": "0",
        "OTHER_FEE3": "0",
        "SUM_OTHER_FEE": "0",
        "USE_COLLAR": "0",
    }


def _apply_page_pricing_context(
    base_data: Dict[str, Any],
    pricing_context: Dict[str, Any],
) -> None:
    for field in PAGE_PRICING_CONTEXT_FIELDS:
        if field not in pricing_context:
            raise PriceCalcError(f"page pricing context missing: {field}")
        base_data[field] = pricing_context[field]


def _apply_center_route_info(
    base_data: Dict[str, Any],
    send_site_info: Dict[str, Any],
    send_center: Dict[str, Any],
    dest_center: Dict[str, Any],
    route_name: str,
    dispatch: Dict[str, str],
) -> None:
    send_center_code, send_center_name = _center_code_name(send_center)
    dest_center_code, dest_center_name = _center_code_name(dest_center)

    if send_center_code:
        base_data["SEND_CENTER_CODE"] = send_center_code
    if send_center_name:
        base_data["SEND_CENTER"] = send_center_name

    if dest_center_code:
        base_data["DESTINATION_CENTER_CODE"] = dest_center_code
    if dest_center_name:
        base_data["DESTINATION_CENTER"] = dest_center_name

    if route_name:
        base_data["ROUTE_NAME"] = route_name

    if dispatch.get("dispatch_site_code"):
        base_data.setdefault("DISP_UNDERLING_SITE_CODE", dispatch.get("dispatch_site_code"))

    if send_site_info:
        if _clean_str(send_site_info.get("SUPERIOR_FINANCE_CENTER")):
            base_data["SEND_FINANCE_CENTER"] = _clean_str(send_site_info.get("SUPERIOR_FINANCE_CENTER"))
        if _clean_str(send_site_info.get("SUPERIOR_FINANCE_CENTER_CODE")):
            base_data["SEND_FINANCE_CENTER_CODE"] = _clean_str(send_site_info.get("SUPERIOR_FINANCE_CENTER_CODE"))
        if _clean_str(send_site_info.get("PROVINCE")):
            base_data["SEND_PROVINCE"] = _clean_str(send_site_info.get("PROVINCE"))
        if _clean_str(send_site_info.get("CITY")):
            base_data["SEND_CITY"] = _clean_str(send_site_info.get("CITY"))
        if _clean_str(send_site_info.get("COUNTY")):
            base_data["SEND_COUNTY"] = _clean_str(send_site_info.get("COUNTY"))

    base_data["REC_MAN_CODE"] = base_data.get("TAKE_PIECE_EMPLOYEE_CODE", "")


def build_output(
    prices: Dict[str, float],
    dispatch_prices: Dict[str, float],
    dispatch_site_name: str,
    site_info: Dict[str, Any],
) -> Dict[str, Any]:
    output: Dict[str, Any] = {}
    if dispatch_site_name:
        output["目的网点"] = dispatch_site_name

    for product_name, fee in prices.items():
        output[product_name] = _format_money(fee)

    for product_name, fee in dispatch_prices.items():
        output[f"{product_name}(派送)"] = _format_money(fee)

    site_field_map = {
        "门店地址": "ADDRESS",
        "派送范围": "DISPATCH_RANGE",
        "特殊区域": "SPECSERVICE_RANGE",
        "到件时效": "TIME_OF_ARRIVAL",
        "不派送范围": "NOT_DISPATCH_RANGE",
        "超远乡镇": "EXCEED_TOWN",
        "超远乡镇详情": "EXCEED_TOWN_DETAIL",
        "查询电话": "PHONE",
        "经理电话": "MANAGER_PHONE",
    }

    for out_key, src_key in site_field_map.items():
        value = site_info.get(src_key)
        if _skip_value(value):
            continue
        output[out_key] = _clean_str(value)

    return output


def fetch_prices(address: str, weight: float, volume: float, config_path: Optional[str] = None) -> Dict[str, Any]:
    auth = TMSAuth(config_path)
    auth.config.setdefault("test_user_data", {})
    if DEFAULT_LOGIN_USER:
        auth.config["test_user_data"]["operator_uid"] = DEFAULT_LOGIN_USER
    if DEFAULT_LOGIN_PASS:
        auth.config["test_user_data"]["operator_password"] = DEFAULT_LOGIN_PASS
    session = auth.login_and_get_session()
    if session is None:
        raise PriceCalcError("login failed: no session")

    ctx = _fetch_login_context(session)
    emp_code = ctx.get("emp_code") or auth.config.get("test_user_data", {}).get("operator_uid", "")
    emp_name = ctx.get("emp_name") or ctx.get("site_name") or emp_code

    def _unreachable() -> Dict[str, str]:
        return {"网点不可达": "网点不可达"}

    try:
        resolved = resolve_address_destination(session, address, ctx, emp_code, emp_name)
    except PriceCalcError:
        return _unreachable()

    addr_info = resolved["addr_info"]
    destination = resolved["destination"]
    dispatch = resolved["dispatch"]
    temp_dest_code = resolved["temp_dest_code"]
    used_address = resolved["used_address"]
    pricing_context = resolved["pricing_context"]

    send_site_info = _fetch_site_and_center(session, ctx.get("site_code", ""))
    send_center = _fetch_send_center(session, ctx.get("site_code", ""))
    send_center_code, _ = _center_code_name(send_center)
    dest_center = _fetch_destination_center(session, dispatch.get("dispatch_site_code", ""), send_center_code)
    dest_center_code, _ = _center_code_name(dest_center)
    route_name = _fetch_plan_route_name(session, send_center_code, dest_center_code)

    weight_ratio = _fetch_weight_ratio(session, ctx.get("site_code", ""), temp_dest_code)
    volume_weight = _volume_weight(volume, weight_ratio)
    settlement_weight = _settlement_weight(weight, volume_weight)

    base_data = _build_base_payload(
        ctx,
        addr_info,
        destination,
        dispatch,
        used_address,
        weight,
        volume,
        volume_weight,
        settlement_weight,
        emp_code,
        emp_name,
    )
    _apply_page_pricing_context(base_data, pricing_context)
    _apply_center_route_info(base_data, send_site_info, send_center, dest_center, route_name, dispatch)

    prices: Dict[str, float] = {}
    dispatch_prices: Dict[str, float] = {}
    for product in _available_products(settlement_weight, use_collar=False):
        base = dict(base_data)
        base["PRODUCT_CODE"] = product["PRODUCT_CODE"]
        base["PRODUCT_TYPE"] = product["PRODUCT_TYPE"]
        base["GOODS_CODE"] = product["PRODUCT_CODE"]

        data_pick = dict(base)
        data_pick["DISPATCH_MODE"] = "自提"
        fee_pick = _calc_price(session, data_pick)
        if fee_pick is not None:
            prices[product["PRODUCT_TYPE"]] = fee_pick

        data_dispatch = dict(base)
        data_dispatch["DISPATCH_MODE"] = "派送"
        fee_dispatch = _calc_price(session, data_dispatch)
        if fee_dispatch is not None:
            dispatch_prices[product["PRODUCT_TYPE"]] = fee_dispatch

    site_info = {}
    if dispatch.get("dispatch_site_code"):
        site_info = _fetch_site_info(session, dispatch["dispatch_site_code"])

    return build_output(prices, dispatch_prices, dispatch.get("dispatch_site_name", ""), site_info)


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fetch price + site info for an address.")
    parser.add_argument("address", help="Receiver address")
    parser.add_argument("weight", type=float, help="Weight (kg)")
    parser.add_argument("volume", nargs="?", type=float, default=0.1, help="Volume (m^3), default 0.1")
    parser.add_argument("--config", default=None, help="Path to config.json")
    parser.add_argument("--out", default="", help="Optional output file")
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.ERROR,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )

    try:
        result = fetch_prices(args.address, args.weight, args.volume, config_path=args.config)
    except Exception as exc:
        logging.error("failed: %s", exc)
        raise

    output = json.dumps(result, ensure_ascii=False)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            handle.write(output)
            handle.write("\n")
    else:
        print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


def run_once(params: Dict[str, Any]) -> Any:
    address = _clean_str((params or {}).get("address"))
    weight = float((params or {}).get("weight", 0))
    raw_volume = (params or {}).get("volume", None)
    if raw_volume is None or raw_volume == "":
        volume = 0.1
    else:
        volume = float(raw_volume)
    config_path = (params or {}).get("config")
    return fetch_prices(address, weight, volume, config_path=config_path)
