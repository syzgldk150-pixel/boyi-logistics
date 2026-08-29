from __future__ import annotations

import datetime as dt
import hashlib
import json
import mimetypes
import os
import re
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urljoin, urlparse

import httpx

from agent.tms_runtime.scripts.login_manager import TMSAuth

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

BASE_URL = "https://tms.ronghuiwl.com"
MENU_URL = f"{BASE_URL}/menuTreeExtend/loadMenu"
INDEX_URL = f"{BASE_URL}/module/index?mv=index"
DATA_QUERY_URL = f"{BASE_URL}/dataQuery/findAllByCallId"
SAVE_TABLES_URL = f"{BASE_URL}/dataOperation/saveTables"
UPLOAD_URL = f"{BASE_URL}/file/upload?sysFileUploadId=ALL"

SCRIPTS_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPTS_DIR.parents[2]

from agent.tms_runtime.account_manager import get_account_manager
from agent.tms_runtime.scripts.ronghui_problem_upload import upload_problem_item

DEFAULT_SPREADSHEET_TOKEN = "F0NVsI5dlhaWugtw14YcmdrQnvh"
DEFAULT_SHEET_ID = "UeBd3I"
SOURCE_SELF_PICKUP_DEPARTMENT = "self_pickup_department"
SOURCE_DAXIANG_S_SELF_PICKUP = "daxiang_s_self_pickup"
DEFAULT_SOURCE_SHEET_TITLE = "每日到货表"
DEFAULT_DESTINATION_SITE = "邵阳自提部"
DAXIANG_S_DESTINATION_SITE = "邵阳大祥S站"
DAXIANG_S_DELIVERY_METHOD = "自提"
DEFAULT_PROBLEM_TYPE = "开单为自提件"
DEFAULT_PROBLEM_OWNER_TYPE = "特殊时效"
DEFAULT_PROBLEM_CAUSE = (
    "货已到，尽快安排提货，自提部免费仓储只有1天，尽快提走，"
    "超时产生仓储费0.03元/KG/天10元票/天；自提电话：0739-5186128 "
    "地址：双清区建设南路白马田伟业物流城内融辉物流(导航：勇胜物流)；"
    "托盘类、少量件数类货物提货时间:9:00-20:00；"
    "件数多的需要装卸工操作的货物提货时间10:00-20:00；"
)
DAXIANG_S_PROBLEM_CAUSE = (
    "货已到，尽快安排提货，网点免费仓储只有3天，尽快提走，"
    "超时产生仓储费0.03元/KG/天10元票/天；自提电话：0739-5186128 "
    "地址：双清区建设南路白马田伟业物流城内融辉物流(导航：勇胜物流)；"
    "托盘类、少量件数类货物提货时间:9:00-20:00；"
    "件数多的需要装卸工操作的货物提货时间10:00-20:00"
)

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}
DEFAULT_HEADERS = {
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Origin": BASE_URL,
    "Referer": f"{BASE_URL}/widget/home",
    "X-Requested-With": "XMLHttpRequest",
}


def _json_datetime(value: dt.datetime | None = None) -> str:
    value = value or dt.datetime.now(dt.timezone.utc).astimezone()
    return value.astimezone(dt.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _clean_text(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    if text.endswith(".0") and text[:-2].isdigit():
        return text[:-2]
    return text


def _parse_count(value: Any) -> Decimal | None:
    text = _clean_text(value).replace(",", "")
    if not text:
        return None
    match = re.search(r"\d+(?:\.\d+)?", text)
    if not match:
        return None
    try:
        return Decimal(match.group(0))
    except InvalidOperation:
        return None


def _is_arrival_complete(arrival_count: Any, goods_count: Any) -> bool:
    arrival = _parse_count(arrival_count)
    goods = _parse_count(goods_count)
    if arrival is None or goods is None:
        return False
    return arrival == goods


def _bool_param(params: dict[str, Any], key: str, default: bool = False) -> bool:
    value = params.get(key, default)
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "y", "on"}:
            return True
        if lowered in {"0", "false", "no", "n", "off", ""}:
            return False
    return bool(value)


def _required_role_value(params: dict[str, Any], key: str) -> str:
    value = _clean_text(params.get(key))
    if not value:
        raise ValueError(f"项目设置必须显式绑定 {key}")
    return value


def _normalize_bill_code(value: Any, *, label: str = "运单号") -> str:
    text = _clean_text(value)
    if text.startswith("="):
        text = text[1:].strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {"'", '"'}:
        text = text[1:-1].strip()
    if any(character.isspace() for character in text):
        raise ValueError(f"{label}包含内部空白，不能自动删除或拼接")
    return text


def _extract_input_value(html: str, input_id: str) -> str:
    pattern = rf"id=[\"']{re.escape(input_id)}[\"'][^>]*value=[\"']([^\"']*)[\"']"
    match = re.search(pattern, html)
    return match.group(1) if match else ""


def _js_unescape(value: str) -> str:
    def _replace_unicode(match: re.Match[str]) -> str:
        return chr(int(match.group(1), 16))

    text = re.sub(r"%u([0-9A-Fa-f]{4})", _replace_unicode, value or "")
    return unquote(text)


def _read_user_info_cookie(session: Any) -> dict[str, Any]:
    raw = session.cookies.get("userInfo")
    if not raw:
        return {}
    try:
        decoded = _js_unescape(raw)
        payload = json.loads(decoded)
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _fetch_login_context(session: Any) -> dict[str, str]:
    response = session.get(INDEX_URL, timeout=20)
    response.raise_for_status()
    html = response.text
    user_info = _read_user_info_cookie(session)
    return {
        "site_code": _extract_input_value(html, "loginSiteCode") or _clean_text(user_info.get("loginSiteCode")),
        "site_name": _extract_input_value(html, "loginSiteName") or _clean_text(user_info.get("loginSiteName")),
        "emp_code": _clean_text(user_info.get("loginEmpCode")),
        "emp_name": _clean_text(user_info.get("loginEmpName")),
        "user_id": _clean_text(user_info.get("loginUserId") or user_info.get("loginEmpCode")),
        "user_name": _clean_text(user_info.get("loginUserName") or user_info.get("loginEmpName")),
        "dept_name": _clean_text(user_info.get("loginEmpDeptName")),
    }


def _tenant_access_token() -> str:
    app_id = os.getenv("FEISHU_APP_ID")
    app_secret = os.getenv("FEISHU_APP_SECRET")
    if not app_id or not app_secret:
        raise RuntimeError("FEISHU_APP_ID 或 FEISHU_APP_SECRET 未配置")
    response = httpx.post(
        "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
        json={"app_id": app_id, "app_secret": app_secret},
        timeout=20,
    )
    response.raise_for_status()
    payload = response.json()
    if payload.get("code") not in (0, None):
        raise RuntimeError(f"飞书鉴权失败: {payload.get('msg') or payload}")
    token = payload.get("tenant_access_token")
    if not token:
        raise RuntimeError("飞书鉴权返回缺少 tenant_access_token")
    return str(token)


def _feishu_get(token: str, path: str) -> dict[str, Any]:
    response = httpx.get(
        f"https://open.feishu.cn{path}",
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
    )
    response.raise_for_status()
    payload = response.json()
    if payload.get("code") not in (0, None):
        raise RuntimeError(f"飞书 OpenAPI 失败: {payload.get('msg') or payload}")
    return payload if isinstance(payload, dict) else {}


def _list_sheets(token: str, spreadsheet_token: str) -> list[dict[str, Any]]:
    payload = _feishu_get(token, f"/open-apis/sheets/v3/spreadsheets/{spreadsheet_token}/sheets/query")
    sheets = payload.get("data", {}).get("sheets", [])
    return [item for item in sheets if isinstance(item, dict)]


def _resolve_sheet_id(
    token: str,
    spreadsheet_token: str,
    *,
    sheet_id: str,
    sheet_title: str,
) -> tuple[str, str]:
    sheets = _list_sheets(token, spreadsheet_token)
    if sheet_title:
        for item in sheets:
            if _clean_text(item.get("title")) == sheet_title:
                return _clean_text(item.get("sheet_id")), sheet_title
    if sheet_id:
        for item in sheets:
            if _clean_text(item.get("sheet_id")) == sheet_id:
                return sheet_id, _clean_text(item.get("title"))
        return sheet_id, ""
    raise RuntimeError("未指定飞书 sheet_id 或 source_sheet_title")


def _read_sheet_values(
    token: str,
    spreadsheet_token: str,
    sheet_id: str,
    *,
    end_col: str,
    max_rows: int,
) -> list[list[Any]]:
    value_range = quote(f"{sheet_id}!A1:{end_col}{max_rows}", safe="")
    payload = _feishu_get(
        token,
        f"/open-apis/sheets/v2/spreadsheets/{spreadsheet_token}/values/{value_range}"
        "?valueRenderOption=FormattedValue",
    )
    values = payload.get("data", {}).get("valueRange", {}).get("values", [])
    return [row if isinstance(row, list) else [] for row in values]


def _header_index(headers: list[Any], candidates: tuple[str, ...], default: int) -> int:
    normalized = [_clean_text(value) for value in headers]
    for candidate in candidates:
        for index, value in enumerate(normalized):
            if value == candidate or candidate in value:
                return index
    return default


def _source_rules(params: dict[str, Any]) -> list[dict[str, Any]]:
    problem_type = _clean_text(params.get("problem_type") or DEFAULT_PROBLEM_TYPE)
    problem_owner_type = _clean_text(params.get("problem_owner_type") or DEFAULT_PROBLEM_OWNER_TYPE)
    account_id = _required_role_value(params, "account_id")
    session_profile = _required_role_value(params, "session_profile")
    rules = [
        {
            "source_id": SOURCE_SELF_PICKUP_DEPARTMENT,
            "source_name": DEFAULT_DESTINATION_SITE,
            "destination_site": _clean_text(params.get("destination_site") or DEFAULT_DESTINATION_SITE),
            "delivery_method": "",
            "account_id": account_id,
            "session_profile": session_profile,
            "problem_type": problem_type,
            "problem_owner_type": problem_owner_type,
            "problem_cause": _clean_text(params.get("problem_cause") or DEFAULT_PROBLEM_CAUSE),
        }
    ]
    if _bool_param(params, "include_daxiang_s_self_pickup", True):
        daxiang_account_id = _required_role_value(params, "daxiang_s_account_id")
        daxiang_session_profile = _required_role_value(
            params,
            "daxiang_s_session_profile",
        )
        rules.append(
            {
                "source_id": SOURCE_DAXIANG_S_SELF_PICKUP,
                "source_name": "邵阳大祥S站自提",
                "destination_site": _clean_text(params.get("daxiang_s_destination_site") or DAXIANG_S_DESTINATION_SITE),
                "delivery_method": _clean_text(params.get("daxiang_s_delivery_method") or DAXIANG_S_DELIVERY_METHOD),
                "account_id": daxiang_account_id,
                "session_profile": daxiang_session_profile,
                "problem_type": _clean_text(params.get("daxiang_s_problem_type") or problem_type),
                "problem_owner_type": _clean_text(params.get("daxiang_s_problem_owner_type") or problem_owner_type),
                "problem_cause": _clean_text(params.get("daxiang_s_problem_cause") or DAXIANG_S_PROBLEM_CAUSE),
            }
        )
    return rules


def _public_source_rule(rule: dict[str, Any]) -> dict[str, Any]:
    return {
        "source_id": rule.get("source_id"),
        "source_name": rule.get("source_name"),
        "destination_site": rule.get("destination_site"),
        "delivery_method": rule.get("delivery_method"),
        "account_id": rule.get("account_id"),
        "session_profile": rule.get("session_profile"),
    }


def _resolve_bound_account_roles(params: dict[str, Any]) -> dict[str, Any]:
    _required_role_value(params, "account_id")
    include_daxiang = _bool_param(params, "include_daxiang_s_self_pickup", True)
    if include_daxiang:
        _required_role_value(params, "daxiang_s_account_id")
    manager = get_account_manager()
    resolved = manager.resolve_role_account_params(
        params,
        account_field="account_id",
        output_session_profile_field="session_profile",
    )
    if not include_daxiang:
        return resolved
    return manager.resolve_role_account_params(
        resolved,
        account_field="daxiang_s_account_id",
        output_session_profile_field="daxiang_s_session_profile",
    )


def _collect_waybills_from_values(
    values: list[list[Any]],
    *,
    destination_site: str = "",
    source_rules: list[dict[str, Any]] | None = None,
    source_sheet_id: str,
    source_sheet_title: str,
) -> list[dict[str, Any]]:
    if not values:
        return []
    headers = values[0]
    bill_col = _header_index(headers, ("运单编号", "单号"), 0)
    dest_col = _header_index(headers, ("目的站点", "目的网点"), 9)
    delivery_col = _header_index(headers, ("派送方式", "送货方式", "配送方式"), -1)
    arrival_count_col = _header_index(headers, ("累计到货件数", "已到货件数", "到货件数"), -1)
    goods_count_col = _header_index(headers, ("货物件数", "货物总件数", "总货物件数", "开单件数", "应到件数", "件数"), -1)
    if source_rules is None:
        raise ValueError("source_rules 必须包含显式账号绑定")
    if any(_clean_text(rule.get("delivery_method")) for rule in source_rules) and delivery_col < 0:
        raise RuntimeError("每日到货表缺少派送方式列，无法筛选邵阳大祥S站自提单号")
    if arrival_count_col < 0 or goods_count_col < 0 or arrival_count_col == goods_count_col:
        raise RuntimeError("每日到货表缺少累计到货件数或货物件数列，无法确认货物是否到齐")

    records: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for row_number, row in enumerate(values[1:], start=2):
        destination = _clean_text(row[dest_col] if len(row) > dest_col else "")
        delivery_method = _clean_text(row[delivery_col] if delivery_col >= 0 and len(row) > delivery_col else "")
        matched_rules: list[dict[str, Any]] = []
        for rule in source_rules:
            rule_destination = _clean_text(rule.get("destination_site"))
            if rule_destination and destination and destination != rule_destination:
                continue
            if rule_destination and not destination and dest_col < len(headers):
                continue
            rule_delivery = _clean_text(rule.get("delivery_method"))
            if rule_delivery and delivery_method != rule_delivery:
                continue
            matched_rules.append(rule)
        if not matched_rules:
            continue
        if len(matched_rules) != 1:
            raise ValueError(f"每日到货表第 {row_number} 行同时匹配多个自提来源")
        bill = _normalize_bill_code(
            row[bill_col] if len(row) > bill_col else "",
            label=f"每日到货表第 {row_number} 行运单号",
        )
        if not bill:
            continue
        arrival_count = _clean_text(row[arrival_count_col] if len(row) > arrival_count_col else "")
        goods_count = _clean_text(row[goods_count_col] if len(row) > goods_count_col else "")
        if not _is_arrival_complete(arrival_count, goods_count):
            continue
        for rule in matched_rules:
            source_id = _clean_text(rule.get("source_id")) or "default"
            seen_key = (source_id, bill)
            if seen_key in seen:
                continue
            seen.add(seen_key)
            records.append(
                {
                    "bill_code": bill,
                    "destination_site": destination,
                    "delivery_method": delivery_method,
                    "arrival_count": arrival_count,
                    "goods_count": goods_count,
                    "row_number": row_number,
                    "source_sheet_id": source_sheet_id,
                    "source_sheet_title": source_sheet_title,
                    "source_id": source_id,
                    "source_name": rule.get("source_name"),
                    "account_id": rule.get("account_id"),
                    "session_profile": rule.get("session_profile"),
                    "problem_type": rule.get("problem_type"),
                    "problem_owner_type": rule.get("problem_owner_type"),
                    "problem_cause": rule.get("problem_cause"),
                }
            )
    return records


def _read_feishu_waybills(params: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    token = _tenant_access_token()
    spreadsheet_token = _clean_text(params.get("spreadsheet_token") or DEFAULT_SPREADSHEET_TOKEN)
    source_rules = _source_rules(params)
    source_sheet_title = _clean_text(params.get("source_sheet_title") or DEFAULT_SOURCE_SHEET_TITLE)
    sheet_id = _clean_text(params.get("sheet_id") or DEFAULT_SHEET_ID)
    max_rows = int(params.get("max_rows") or 2000)
    end_col = _clean_text(params.get("end_col") or "S")

    resolved_sheet_id, resolved_title = _resolve_sheet_id(
        token,
        spreadsheet_token,
        sheet_id=sheet_id,
        sheet_title=source_sheet_title,
    )
    values = _read_sheet_values(
        token,
        spreadsheet_token,
        resolved_sheet_id,
        end_col=end_col,
        max_rows=max_rows,
    )
    records = _collect_waybills_from_values(
        values,
        source_rules=source_rules,
        source_sheet_id=resolved_sheet_id,
        source_sheet_title=resolved_title,
    )

    if not records and resolved_sheet_id != sheet_id:
        fallback_values = _read_sheet_values(
            token,
            spreadsheet_token,
            sheet_id,
            end_col=end_col,
            max_rows=max_rows,
        )
        records = _collect_waybills_from_values(
            fallback_values,
            source_rules=source_rules,
            source_sheet_id=sheet_id,
            source_sheet_title="",
        )
        resolved_sheet_id = sheet_id
        resolved_title = ""

    limit = params.get("limit")
    if limit not in (None, ""):
        records = records[: max(0, int(limit))]

    source = {
        "spreadsheet_token": spreadsheet_token,
        "sheet_id": resolved_sheet_id,
        "sheet_title": resolved_title,
        "destination_site": "、".join(
            dict.fromkeys(
                _clean_text(rule.get("destination_site"))
                for rule in source_rules
                if _clean_text(rule.get("destination_site"))
            )
        ),
        "source_rules": [_public_source_rule(rule) for rule in source_rules],
    }
    return records, source


def _candidate_preview(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "bill_code": item["bill_code"],
            "row_number": item.get("row_number"),
            "destination_site": item.get("destination_site"),
            "delivery_method": item.get("delivery_method"),
            "arrival_count": item.get("arrival_count"),
            "goods_count": item.get("goods_count"),
            "source_id": item.get("source_id"),
            "source_name": item.get("source_name"),
            "account_id": item.get("account_id"),
            "session_profile": item.get("session_profile"),
        }
        for item in records
    ]


def _canonical_preview_count(value: Any, label: str) -> str:
    number = _parse_count(value)
    if number is None or not number.is_finite() or number < 0:
        raise RuntimeError(f"{label} is not a complete numeric count")
    return (
        str(number.quantize(Decimal("1")))
        if number == number.to_integral_value()
        else format(number.normalize(), "f")
    )


def _preview_fingerprint(records: list[dict[str, Any]]) -> str:
    material = [
        {
            "arrival_count": _canonical_preview_count(
                item.get("arrival_count"),
                f"{item.get('bill_code')} arrival count",
            ),
            "bill_code": _clean_text(item.get("bill_code")),
            "delivery_method": _clean_text(item.get("delivery_method")),
            "destination_site": _clean_text(item.get("destination_site")),
            "goods_count": _canonical_preview_count(
                item.get("goods_count"),
                f"{item.get('bill_code')} goods count",
            ),
            "problem_cause_sha256": hashlib.sha256(
                _clean_text(item.get("problem_cause")).encode("utf-8")
            ).hexdigest(),
            "problem_owner_type": _clean_text(item.get("problem_owner_type")),
            "problem_type": _clean_text(item.get("problem_type")),
            "source_id": _clean_text(item.get("source_id")),
        }
        for item in records
    ]
    encoded = json.dumps(
        material,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _records_by_source(records: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        source_id = _clean_text(record.get("source_id")) or "default"
        grouped.setdefault(source_id, []).append(record)
    return grouped


def _source_summaries(
    source_rules: list[dict[str, Any]],
    records: list[dict[str, Any]],
    runtime_stats: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    grouped = _records_by_source(records)
    runtime_stats = runtime_stats or {}
    summaries: list[dict[str, Any]] = []
    for rule in source_rules:
        source_id = _clean_text(rule.get("source_id")) or "default"
        group = grouped.get(source_id, [])
        stats = runtime_stats.get(source_id, {})
        summary = _public_source_rule(rule)
        summary.update(
            {
                "candidate_count": len(group),
                "candidates": _candidate_preview(group),
                "saved_bills": int(stats.get("saved_bills") or 0),
                "skipped_bills": int(stats.get("skipped_bills") or 0),
                "failed_bills": int(stats.get("failed_bills") or 0),
                "uploaded_files_total": int(stats.get("uploaded_files_total") or 0),
            }
        )
        summaries.append(summary)
    return summaries


def _walk_menu(nodes: list[dict[str, Any]], path: str = ""):
    for node in nodes or []:
        text = _clean_text(node.get("text") or node.get("name"))
        new_path = f"{path}/{text}" if path and text else text or path
        yield node, new_path
        children = node.get("children") or []
        if isinstance(children, list):
            yield from _walk_menu(children, new_path)


def _resolve_problem_page_context(session: Any) -> dict[str, str]:
    response = session.get(MENU_URL, timeout=20)
    response.raise_for_status()
    payload = response.json()
    nodes = payload.get("result", {}).get("data") if isinstance(payload, dict) else []
    candidates: list[str] = []
    for node, path in _walk_menu(nodes if isinstance(nodes, list) else []):
        text = _clean_text(node.get("text") or node.get("name"))
        url = _clean_text(node.get("url"))
        if not url or "/widget/home" not in url:
            continue
        if text == "问题件录入" or path.endswith("/问题件录入"):
            candidates.insert(0, url)
        elif "问题件录入" in path:
            candidates.append(url)

    for url in candidates:
        full_url = urljoin(BASE_URL, url)
        parsed = urlparse(full_url)
        query = parse_qs(parsed.query)
        auth_key = (query.get("authenticationKey") or [""])[0]
        page_id = (query.get("pageId") or [""])[0]
        html = session.get(full_url, timeout=20).text
        auth_key = auth_key or _extract_between(html, 'authenticationKey:"', '"')
        page_id = page_id or _extract_between(html, 'pageId:"', '"')
        if "TAB_PROBLEM_ADD" in html or "问题件录入" in html:
            return {
                "url": full_url,
                "authentication_key": auth_key,
                "page_id": page_id,
            }
    raise RuntimeError("无法定位 TMS 问题件录入页面")


def _extract_between(text: str, start_token: str, end_token: str) -> str:
    start = text.find(start_token)
    if start == -1:
        return ""
    start += len(start_token)
    end = text.find(end_token, start)
    if end == -1:
        return ""
    return text[start:end]


def _headers(page_context: dict[str, str], *, content_type: str | None = None) -> dict[str, str]:
    headers = dict(DEFAULT_HEADERS)
    headers["Referer"] = page_context.get("url") or f"{BASE_URL}/widget/home"
    if page_context.get("authentication_key"):
        headers["authenticationKey"] = page_context["authentication_key"]
    if page_context.get("page_id"):
        headers["pageId"] = page_context["page_id"]
    if content_type:
        headers["Content-Type"] = content_type
    return headers


def _query_rows(
    session: Any,
    call_id: str,
    data: dict[str, Any] | None = None,
    *,
    page_context: dict[str, str],
) -> list[dict[str, Any]]:
    response = session.post(
        f"{DATA_QUERY_URL}?id={call_id}",
        data=data or {},
        headers=_headers(page_context, content_type="application/x-www-form-urlencoded; charset=UTF-8"),
        timeout=30,
    )
    response.raise_for_status()
    payload = response.json()
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict) and isinstance(payload.get("data"), list):
        return [item for item in payload["data"] if isinstance(item, dict)]
    return []


def _fetch_guid(session: Any, page_context: dict[str, str]) -> str:
    rows = _query_rows(session, "FIND_GUID", page_context=page_context)
    guid = _clean_text(rows[0].get("GUID")) if rows else ""
    if not guid:
        raise RuntimeError("TMS FIND_GUID 未返回 GUID")
    return guid


def _fetch_bill_info(session: Any, bill_code: str, page_context: dict[str, str]) -> dict[str, Any]:
    call_id = "FIND_BILL_BY_BILL_CODE_RH" if bill_code.startswith("H") else "FIND_BILL_BY_BILL_CODE_NEW"
    rows = _query_rows(session, call_id, {"BILL_CODE": bill_code}, page_context=page_context)
    if not rows:
        raise RuntimeError("TMS 未查到运单")
    return rows[0]


def _resolve_notice_site(bill_info: dict[str, Any], login_context: dict[str, str]) -> tuple[str, str]:
    login_site_code = _clean_text(login_context.get("site_code"))
    send_site_code = _clean_text(bill_info.get("SEND_SITE_CODE"))
    register_site_code = _clean_text(bill_info.get("REGISTER_SITE_CODE") or bill_info.get("SEND_SITE_CODE"))
    register_site = _clean_text(bill_info.get("REGISTER_SITE") or bill_info.get("SEND_SITE"))
    if login_site_code in {send_site_code, register_site_code}:
        return _clean_text(bill_info.get("DISPATCH_SITE_CODE")), _clean_text(bill_info.get("DISPATCH_SITE"))
    return register_site_code, register_site


def _has_explicit_screenshot_param(params: dict[str, Any]) -> bool:
    return bool(
        _clean_text(params.get("screenshot_path"))
        or _clean_text(params.get("screenshot_dir"))
        or (isinstance(params.get("screenshot_map"), dict) and params.get("screenshot_map"))
    )


def _should_upload_screenshot(params: dict[str, Any]) -> bool:
    return _bool_param(params, "upload_screenshot", False) or _has_explicit_screenshot_param(params)


def _resolve_screenshot_path(params: dict[str, Any], bill_code: str, *, include_env: bool = False) -> str:
    screenshot_map = params.get("screenshot_map")
    if isinstance(screenshot_map, dict):
        mapped = screenshot_map.get(bill_code)
        if mapped:
            return _clean_text(mapped)

    screenshot_dir = _clean_text(params.get("screenshot_dir"))
    if screenshot_dir:
        root = Path(screenshot_dir).expanduser()
        if root.exists() and root.is_dir():
            exact_matches = sorted(
                path for path in root.glob(f"{bill_code}.*") if path.suffix.lower() in IMAGE_EXTS
            )
            if exact_matches:
                return str(exact_matches[0])
            fuzzy_matches = sorted(
                path for path in root.iterdir()
                if path.is_file() and path.suffix.lower() in IMAGE_EXTS and bill_code in path.stem
            )
            if fuzzy_matches:
                return str(fuzzy_matches[0])

    fallback = params.get("screenshot_path")
    if include_env:
        fallback = (
            fallback
            or os.getenv("HUOLALA_ORDER_SCREENSHOT_PATH")
            or os.getenv("TMS_SELF_PICKUP_PROBLEM_SCREENSHOT_PATH")
        )
    return _clean_text(fallback)


def _validate_image_path(path: str) -> str:
    if not path:
        raise RuntimeError("缺少货拉拉订单截图路径，请传 screenshot_path/screenshot_dir 或配置 HUOLALA_ORDER_SCREENSHOT_PATH")
    resolved = Path(path).expanduser()
    if not resolved.exists() or not resolved.is_file():
        raise RuntimeError(f"货拉拉订单截图不存在: {resolved}")
    if resolved.suffix.lower() not in IMAGE_EXTS:
        raise RuntimeError(f"不支持的截图格式: {resolved.suffix}")
    return str(resolved)


def _upload_image(session: Any, image_path: str, page_context: dict[str, str]) -> dict[str, str]:
    image_path = _validate_image_path(image_path)
    mime_type = mimetypes.guess_type(image_path)[0] or "application/octet-stream"
    headers = _headers(page_context)
    headers.pop("Content-Type", None)
    with open(image_path, "rb") as handle:
        response = session.post(
            UPLOAD_URL,
            files={"file": (os.path.basename(image_path), handle, mime_type)},
            headers=headers,
            timeout=60,
        )
    response.raise_for_status()
    payload = response.json()
    if not payload.get("success"):
        raise RuntimeError(f"TMS 图片上传失败: {payload.get('message') or payload}")
    data = payload.get("data")
    if not isinstance(data, list) or not data:
        raise RuntimeError(f"TMS 图片上传返回异常: {payload}")
    item = data[0]
    file_dir = _clean_text(item.get("fileDir"))
    file_name = _clean_text(item.get("fileName") or os.path.basename(image_path))
    if not file_dir:
        raise RuntimeError(f"TMS 图片上传返回缺少 fileDir: {payload}")
    return {
        "file_path": f"/unauth/download/{file_dir}",
        "file_name": file_name,
        "local_path": image_path,
    }


def _save_tables(session: Any, operations: list[dict[str, Any]], page_context: dict[str, str]) -> dict[str, Any]:
    params_json = json.dumps(operations, ensure_ascii=False, separators=(",", ":"))
    headers = _headers(page_context)
    headers.pop("Content-Type", None)
    original_content_type = session.headers.pop("Content-Type", None)
    try:
        response = session.post(
            SAVE_TABLES_URL,
            files={"params": (None, params_json)},
            headers=headers,
            timeout=60,
        )
    finally:
        if original_content_type is not None:
            session.headers["Content-Type"] = original_content_type
    response.raise_for_status()
    if not response.content:
        return {"success": True, "message": ""}
    try:
        return response.json()
    except Exception:
        return {"success": False, "message": response.text[:500]}


def _operation(operation_key: str, data: list[dict[str, Any]] | dict[str, Any]) -> dict[str, Any]:
    rows = data if isinstance(data, list) else [data]
    return {
        "beforeAction": None,
        "operationKey": operation_key,
        "afterAction": None,
        "idFields": [],
        "data": rows,
    }


def _build_pic_row(
    *,
    bill_code: str,
    guid: str,
    upload: dict[str, str],
    login_context: dict[str, str],
) -> dict[str, Any]:
    return {
        "BILL_CODE": bill_code,
        "OUT_GUID": guid,
        "SAVE_POS": upload["file_path"],
        "FILE_NAME": upload["file_name"],
        "PIC_TYPE": 3,
        "CREATE_DATE": _json_datetime(),
        "SCAN_SITE": login_context.get("site_name", ""),
        "SCAN_SITE_CODE": login_context.get("site_code", ""),
        "SCAN_MAN": login_context.get("user_name") or login_context.get("emp_name", ""),
        "SCAN_MAN_CODE": login_context.get("user_id") or login_context.get("emp_code", ""),
        "DATA_FROM": "K13",
    }


def _build_problem_row(
    *,
    bill_code: str,
    guid: str,
    notice_site_code: str,
    notice_site: str,
    login_context: dict[str, str],
    problem_cause: str,
    problem_type: str,
    problem_owner_type: str,
) -> dict[str, Any]:
    return {
        "localFilePath": "",
        "TRANSFER_CODE": "",
        "BILL_STATUS": "",
        "EMPLOYEE_CODE": "",
        "OPERATION_EMPLOYEE": "",
        "IS_SEND_SITE_REGISTER": 0,
        "FILE1": "",
        "FILE2": "",
        "FILE3": "",
        "FILE4": "",
        "GUID": guid,
        "BILL_CODE": bill_code,
        "TYPE": problem_type,
        "OWNER_PROBELM_TYPE": problem_owner_type,
        "SEND_SITE_CODE": notice_site_code,
        "SEND_SITE": notice_site,
        "PROBLEM_CAUSE": problem_cause,
        "FILE_PATH": None,
        "FILE_PATH2": "",
        "FILE_PATH3": "",
        "FILE_PATH4": "",
        "REGISTER_SITE": login_context.get("site_name", ""),
        "REGISTER_SITE_CODE": login_context.get("site_code", ""),
        "REGISTER_MAN_DEPT": login_context.get("dept_name", ""),
        "REGISTER_DATE": _json_datetime(),
        "REGISTER_SAVE_DATE": _json_datetime(),
        "REGISTER_MAN": login_context.get("emp_name", ""),
        "REGISTER_MAN_CODE": login_context.get("emp_code", ""),
        "DATA_FROM": "K13",
        "VERIFY_MSG": "",
    }


def _update_postpone_days(session: Any, bill_code: str, page_context: dict[str, str]) -> bool:
    rows = _query_rows(
        session,
        f"FIND_BILL_POSTPONE_DAYS&BILL_CODE={quote(bill_code, safe='')}",
        page_context=page_context,
    )
    if not rows:
        return False
    payload = {
        "BILL_CODE": bill_code,
        "POSTPONE_DAYS": rows[0].get("POSTPONE_DAYS"),
    }
    result = _save_tables(session, [_operation("TAB_BILL_UPT", payload)], page_context)
    return bool(result.get("success", True))


def _fetch_existing_problem_rows(session: Any, bill_code: str, page_context: dict[str, str]) -> list[dict[str, Any]]:
    return _query_rows(session, "FIND_PROBLEM_BY_CODE", {"BILL_CODE": bill_code}, page_context=page_context)


def _process_bill(
    session: Any,
    *,
    record: dict[str, Any],
    page_context: dict[str, str],
    login_context: dict[str, str],
    params: dict[str, Any],
    upload_cache: dict[str, dict[str, str]],
) -> dict[str, Any]:
    bill_code = record["bill_code"]
    bill_info = _fetch_bill_info(session, bill_code, page_context)
    notice_site_code, notice_site = _resolve_notice_site(bill_info, login_context)
    if not notice_site_code or not notice_site:
        raise RuntimeError("通知网点自动匹配为空")
    if notice_site_code == login_context.get("site_code"):
        raise RuntimeError("通知网点和登记网点一致，页面规则禁止上传")

    problem_cause = _clean_text(params.get("problem_cause") or record.get("problem_cause") or DEFAULT_PROBLEM_CAUSE)
    problem_type = _clean_text(params.get("problem_type") or record.get("problem_type") or DEFAULT_PROBLEM_TYPE)
    problem_owner_type = _clean_text(
        params.get("problem_owner_type") or record.get("problem_owner_type") or DEFAULT_PROBLEM_OWNER_TYPE
    )

    if not _should_upload_screenshot(params):
        shared_record = dict(record)
        shared_record.update(
            {
                "problem_cause": problem_cause,
                "problem_type": problem_type,
                "problem_owner_type": problem_owner_type,
            }
        )
        result = upload_problem_item(
            session,
            record=shared_record,
            page_context=page_context,
            login_context=login_context,
            update_postpone=_bool_param(params, "update_postpone_days", True),
            helpers={
                "fetch_bill_info": _fetch_bill_info,
                "resolve_notice_site": _resolve_notice_site,
                "fetch_guid": _fetch_guid,
                "build_problem_row": _build_problem_row,
                "operation": _operation,
                "save_tables": _save_tables,
                "update_postpone_days": _update_postpone_days,
            },
        )
        result.update(
            {
                "delivery_method": record.get("delivery_method", ""),
                "source_id": record.get("source_id", ""),
                "source_name": record.get("source_name", ""),
                "image_path": "",
                "uploaded_file": "",
            }
        )
        return result

    image_path = ""
    upload: dict[str, str] | None = None
    if _should_upload_screenshot(params):
        image_path = _validate_image_path(
            _resolve_screenshot_path(
                params,
                bill_code,
                include_env=_bool_param(params, "upload_screenshot", False),
            )
        )
        upload = upload_cache.get(image_path)
        if upload is None:
            upload = _upload_image(session, image_path, page_context)
            upload_cache[image_path] = upload

    guid = _fetch_guid(session, page_context)

    problem_row = _build_problem_row(
        bill_code=bill_code,
        guid=guid,
        notice_site_code=notice_site_code,
        notice_site=notice_site,
        login_context=login_context,
        problem_cause=problem_cause,
        problem_type=problem_type,
        problem_owner_type=problem_owner_type,
    )
    operations = []
    if upload is not None:
        operations.append(
            _operation(
                "TAB_PIC_SCAN_ADD",
                _build_pic_row(
                    bill_code=bill_code,
                    guid=guid,
                    upload=upload,
                    login_context=login_context,
                ),
            )
        )
    operations.append(_operation("TAB_PROBLEM_ADD", problem_row))
    save_result = _save_tables(session, operations, page_context)
    if save_result.get("success") is False:
        raise RuntimeError(_clean_text(save_result.get("message")) or f"TMS 保存失败: {save_result}")

    postpone_updated = False
    if _bool_param(params, "update_postpone_days", True):
        postpone_updated = _update_postpone_days(session, bill_code, page_context)

    return {
        "bill_code": bill_code,
        "saved": True,
        "notice_site": notice_site,
        "notice_site_code": notice_site_code,
        "destination_site": _clean_text(bill_info.get("DESTINATION")),
        "delivery_method": record.get("delivery_method", ""),
        "source_id": record.get("source_id", ""),
        "source_name": record.get("source_name", ""),
        "guid": guid,
        "image_path": image_path,
        "uploaded_file": upload.get("file_name") if upload else "",
        "postpone_updated": postpone_updated,
        "message": _clean_text(save_result.get("message")) or "success",
    }


def run_once(params: dict[str, Any] | None = None) -> dict[str, Any]:
    params = _resolve_bound_account_roles(dict(params or {}))
    dry_run = _bool_param(params, "dry_run", True)
    source_rules = _source_rules(params)
    primary_rule = source_rules[0] if source_rules else {}
    account_id = _clean_text(primary_rule.get("account_id"))
    session_profile = _clean_text(primary_rule.get("session_profile"))

    records, source = _read_feishu_waybills(params)
    preview = _candidate_preview(records)

    screenshot_enabled = _should_upload_screenshot(params)
    configured_screenshot = _clean_text(
        params.get("screenshot_path")
        or params.get("screenshot_dir")
        or ("screenshot_map" if isinstance(params.get("screenshot_map"), dict) and params.get("screenshot_map") else "")
        or (
            os.getenv("HUOLALA_ORDER_SCREENSHOT_PATH")
            or os.getenv("TMS_SELF_PICKUP_PROBLEM_SCREENSHOT_PATH")
            if _bool_param(params, "upload_screenshot", False)
            else ""
        )
    )
    if dry_run:
        summaries = _source_summaries(source_rules, records)
        return {
            "ok": True,
            "stage": "dry_run",
            "message": f"演练：候选 {len(records)} 单，未上传问题件",
            "candidate_count": len(records),
            "candidates": preview,
            "preview_fingerprint": _preview_fingerprint(records),
            "source_summaries": summaries,
            "source": source,
            "account_id": account_id,
            "session_profile": session_profile,
            "screenshot_required": False,
            "screenshot_enabled": screenshot_enabled,
            "screenshot_configured": bool(configured_screenshot),
            "saved_bills": 0,
            "failed_bills": 0,
            "skipped_bills": 0,
            "results": [],
        }

    if not records:
        summaries = _source_summaries(source_rules, records)
        return {
            "ok": True,
            "stage": "no_candidates",
            "message": "飞书未找到目标目的站点单号",
            "candidate_count": 0,
            "candidates": [],
            "source_summaries": summaries,
            "source": source,
            "account_id": account_id,
            "session_profile": session_profile,
            "saved_bills": 0,
            "failed_bills": 0,
            "skipped_bills": 0,
            "results": [],
        }

    results: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    grouped_records = _records_by_source(records)
    runtime_stats: dict[str, dict[str, Any]] = {
        _clean_text(rule.get("source_id")) or "default": {
            "saved_bills": 0,
            "skipped_bills": 0,
            "failed_bills": 0,
            "uploaded_files_total": 0,
        }
        for rule in source_rules
    }
    stop_processing = False

    for rule in source_rules:
        source_id = _clean_text(rule.get("source_id")) or "default"
        group = grouped_records.get(source_id, [])
        if not group or stop_processing:
            continue
        group_params = dict(params)
        group_params.update(
            {
                "account_id": rule.get("account_id"),
                "session_profile": rule.get("session_profile"),
                "problem_type": rule.get("problem_type"),
                "problem_owner_type": rule.get("problem_owner_type"),
                "problem_cause": rule.get("problem_cause"),
            }
        )
        group_session_profile = _clean_text(rule.get("session_profile"))
        if not group_session_profile:
            raise ValueError(f"{source_id} 未解析出 session_profile")
        session = TMSAuth(profile=group_session_profile).login_and_get_session(
            max_attempts=max(1, int(params.get("max_login_attempts") or 6))
        )
        page_context = _resolve_problem_page_context(session)
        login_context = _fetch_login_context(session)
        upload_cache: dict[str, dict[str, str]] = {}
        stats = runtime_stats.setdefault(
            source_id,
            {"saved_bills": 0, "skipped_bills": 0, "failed_bills": 0, "uploaded_files_total": 0},
        )

        for record in group:
            bill_code = record["bill_code"]
            try:
                result = _process_bill(
                    session,
                    record=record,
                    page_context=page_context,
                    login_context=login_context,
                    params=group_params,
                    upload_cache=upload_cache,
                )
                result.setdefault("source_id", source_id)
                result.setdefault("source_name", rule.get("source_name"))
                results.append(result)
                if result.get("saved"):
                    stats["saved_bills"] += 1
                elif result.get("skipped"):
                    stats["skipped_bills"] += 1
            except Exception as exc:
                failed_item = {
                    "bill_code": bill_code,
                    "saved": False,
                    "source_id": source_id,
                    "source_name": rule.get("source_name"),
                    "error": str(exc)[:500],
                }
                failed.append(failed_item)
                results.append(failed_item)
                stats["failed_bills"] += 1
                if _bool_param(params, "stop_on_error", False):
                    stop_processing = True
                    break
        stats["uploaded_files_total"] = len(upload_cache)

    saved_bills = sum(1 for item in results if item.get("saved"))
    skipped_bills = sum(1 for item in results if item.get("skipped"))
    failed_bills = len(failed)
    summaries = _source_summaries(source_rules, records, runtime_stats)
    return {
        "ok": failed_bills == 0,
        "stage": "done" if failed_bills == 0 else "partial_failed",
        "message": f"完成 {saved_bills}/{len(records)} 单",
        "candidate_count": len(records),
        "candidates": preview,
        "source_summaries": summaries,
        "source": source,
        "account_id": account_id,
        "session_profile": session_profile,
        "screenshot_required": False,
        "screenshot_enabled": screenshot_enabled,
        "saved_bills": saved_bills,
        "failed_bills": failed_bills,
        "skipped_bills": skipped_bills,
        "failed_bill_codes": [item["bill_code"] for item in failed],
        "uploaded_files_total": sum(int(item.get("uploaded_files_total") or 0) for item in runtime_stats.values()),
        "results": results,
    }


def main() -> None:
    raw = sys.stdin.read()
    params = json.loads(raw) if raw.strip() else {}
    result = run_once(params)
    print(json.dumps(result, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()
