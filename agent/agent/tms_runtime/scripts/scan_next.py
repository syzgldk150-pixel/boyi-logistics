"""
Submit Ronghui TMS outgoing scans through the 发件扫描 page's own HTTP calls.

The request contract comes from the real page (captured 2026-10-21): the page
checks the bill against ``FIND_TMS_SYS_SHARE_SET`` rules, skips signed bills
via ``FIND_WAYBILL_SIGN_STATE``, looks the next station up with its
site-type specific lookup and uploads ``TAB_SCAN_SEND_ADD`` rows through
``/dataOperation/saveTables``.  No browser is opened, so reminder pop-ups of
the Ronghui index page cannot interfere.  A save acknowledgement is not
business success on its own; the caller reads the send-scan ledger back.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import itertools
import json
import re
import sys
import time
from typing import Any, Dict, Optional
from zoneinfo import ZoneInfo

from agent.tms_runtime.scripts import get_scan
from agent.tms_runtime.scripts.login_manager import TMSAuth
from agent.tms_runtime.scripts.ronghui_problem_upload import (
    BASE_URL,
    DATA_QUERY_URL,
    _headers,
    _json_datetime,
    _read_user_info_cookie,
    _resolve_menu_page_context,
    operation,
    save_tables,
)


DEFAULT_STATION_NAME = ""
DEFAULT_BILL_CODE = ""
DEFAULT_SESSION_PROFILE = "default"
QUERY_TIMEOUT_SEC = 20
PAGE_QUERY_URL = f"{BASE_URL}/dataQuery/findPageByCallId"
RONGHUI_TIMEZONE = ZoneInfo("Asia/Shanghai")

SCAN_MENU_TEXT = "发件扫描"
SCAN_OPERATION_KEY = "TAB_SCAN_SEND_ADD"
BILL_RULE_CODES = "MainBill,AllBill,ReturnBill,SubBill,VehicleCode"
# The page's init() switches the next-station lookup for distribution centres.
CENTER_SITE_TYPE = "分拨中心"
STATION_LOOKUP_CALL_ID = "FIND_SCAN_SEND_SITE_COMBOBOX_NEW"
CENTER_STATION_LOOKUP_CALL_ID = "FIND_SCAN_SEND_SITE_COMBOBOX_BY_CEN1"
STATION_LOOKUP_PAGE_SIZE = 20
LOGIN_CONTEXT_FIELDS = ("loginUserName", "loginUserAccount", "loginSiteName", "loginSiteCode")
SCAN_MAN_CODE_MAX_UTF8_BYTES = 20
SEND_SCAN_RECORD_CALL_ID = "FIND_SEND_SCAN_RECORD"
SEND_SCAN_RECORD_PAGE_SIZE = 500
SEND_SCAN_RECORD_CHUNK_SIZE = 100

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


def _ts() -> str:
    ts = _dt.datetime.now()
    return ts.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def log(message: str) -> None:
    print(f"[{_ts()}] {message}", flush=True)


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
        except Exception as exc:
            raise ValueError("items 必须是有效 JSON") from exc
    if isinstance(raw, dict):
        for key in ("items", "data", "records", "rows"):
            if key in raw:
                return _coerce_items(raw.get(key))
        item = _normalize_item(raw)
        if not item:
            raise ValueError("items 第 1 项缺少 station_name 或 bill_code")
        return [item]
    if isinstance(raw, list):
        items: list[Dict[str, str]] = []
        invalid_indexes: list[int] = []
        for index, entry in enumerate(raw):
            item = _normalize_item(entry)
            if item:
                items.append(item)
            else:
                invalid_indexes.append(index)
        if invalid_indexes:
            indexes = ",".join(str(index) for index in invalid_indexes)
            raise ValueError(f"items 存在格式错误项，索引: {indexes}")
        return items
    raise ValueError("items 必须是对象、数组或 JSON 字符串")


def _load_json_file(path: str) -> Any:
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def _login_context(session: Any) -> dict[str, str]:
    """Read the scan identity from the same cookie ``$Z.user.getUserInfo()`` decodes."""

    user_info = _read_user_info_cookie(session)
    context = {field: str(user_info.get(field) or "").strip() for field in LOGIN_CONTEXT_FIELDS}
    missing = [field for field, value in context.items() if not value]
    if missing:
        raise RuntimeError(f"融辉登录身份缺少字段: {', '.join(missing)}")
    code_bytes = len(context["loginUserAccount"].encode("utf-8"))
    if code_bytes > SCAN_MAN_CODE_MAX_UTF8_BYTES:
        raise RuntimeError(
            f"扫描员编码超过 {SCAN_MAN_CODE_MAX_UTF8_BYTES} 字节: {code_bytes}"
        )
    context["loginSiteType"] = str(user_info.get("loginSiteType") or "").strip()
    return context


def _json_payload(response: Any, label: str) -> Any:
    response.raise_for_status()
    try:
        return response.json()
    except ValueError as exc:
        raise RuntimeError(f"{label} 响应不是 JSON") from exc


def _query_rows(session: Any, call_id: str, data: dict[str, str], page: dict[str, str]) -> list[dict[str, Any]]:
    response = session.post(
        f"{DATA_QUERY_URL}?id={call_id}",
        data=data,
        headers=_headers(page, content_type="application/x-www-form-urlencoded; charset=UTF-8"),
        timeout=QUERY_TIMEOUT_SEC,
    )
    payload = _json_payload(response, call_id)
    if isinstance(payload, dict) and isinstance(payload.get("data"), list):
        payload = payload["data"]
    if not isinstance(payload, list) or any(not isinstance(row, dict) for row in payload):
        raise RuntimeError(f"{call_id} 响应结构异常")
    return payload


def _bill_rules(session: Any, page: dict[str, str]) -> list[re.Pattern[str]]:
    rules: list[re.Pattern[str]] = []
    for row in _query_rows(session, "FIND_TMS_SYS_SHARE_SET", {"SHARE_CODE_IN": BILL_RULE_CODES}, page):
        value = str(row.get("SHARE_VALUE") or "")
        if not value:
            continue
        try:
            rules.append(re.compile(value))
        except re.error as exc:
            raise RuntimeError(f"融辉单号规则无法解析: {row.get('SHARE_CODE')}") from exc
    if not rules:
        raise RuntimeError("融辉未返回单号规则")
    return rules


def _is_signed(session: Any, page: dict[str, str], bill_code: str) -> bool:
    return bool(_query_rows(session, "FIND_WAYBILL_SIGN_STATE", {"BILL_CODE": bill_code}, page))


def _station_code(session: Any, page: dict[str, str], station_name: str, site_type: str) -> str:
    call_id = CENTER_STATION_LOOKUP_CALL_ID if site_type == CENTER_SITE_TYPE else STATION_LOOKUP_CALL_ID
    response = session.get(
        PAGE_QUERY_URL,
        params={
            "id": call_id,
            "pageSize": STATION_LOOKUP_PAGE_SIZE,
            "pageIndex": 0,
            "key": station_name.upper(),
        },
        headers=_headers(page),
        timeout=QUERY_TIMEOUT_SEC,
    )
    payload = _json_payload(response, call_id)
    rows = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise RuntimeError(f"下一站查询响应结构异常: {call_id}")
    # The lookup can list the same site more than once (2026-10-21: 邵阳武冈站
    # twice with one SITE_CODE); one name must still resolve to one site code.
    codes = {
        str(row.get("SITE_CODE") or "").strip()
        for row in rows
        if isinstance(row, dict) and str(row.get("SITE_NAME") or "").strip() == station_name
    }
    if len(codes) != 1:
        raise RuntimeError(f"下一站需唯一精确匹配: {station_name}，实际 {len(codes)} 个")
    code = codes.pop()
    if not code:
        raise RuntimeError(f"下一站缺少网点编号: {station_name}")
    return code


def query_send_scan_records(session: Any, *, site_code: str, bill_codes: list[str]) -> list[dict[str, Any]]:
    """Send-scan ledger rows of exactly these bills, queried as 扫描记录查询 does by 单号.

    With 单号 the page sends only ``BILL_CODE`` (comma separated; the server
    turns it into an IN filter) plus ``LOGIN_SITE_CODE``.  The server ignores
    ``searchOrderInput``, so a date-range query returns every scan of the site,
    and its pages have no stable order: rows repeat or go missing across pages
    (2026-10-23, 626 rows).  Each chunk therefore has to fit one page.
    """

    rows: list[dict[str, Any]] = []
    for start in range(0, len(bill_codes), SEND_SCAN_RECORD_CHUNK_SIZE):
        chunk = bill_codes[start : start + SEND_SCAN_RECORD_CHUNK_SIZE]
        response = session.post(
            get_scan.SCAN_URL,
            params={"id": SEND_SCAN_RECORD_CALL_ID},
            data={
                "BILL_CODE": ",".join(chunk),
                "LOGIN_SITE_CODE": site_code,
                "pageIndex": "0",
                "pageSize": str(SEND_SCAN_RECORD_PAGE_SIZE),
                "sortField": "",
                "sortOrder": "",
            },
            headers=get_scan.build_headers(),
            allow_redirects=False,
            timeout=QUERY_TIMEOUT_SEC,
        )
        result = _json_payload(response, SEND_SCAN_RECORD_CALL_ID)
        total = result.get("total") if isinstance(result, dict) else None
        page_rows = result.get("data") if isinstance(result, dict) else None
        if (
            isinstance(total, bool)
            or not isinstance(total, int)
            or not isinstance(page_rows, list)
            or any(not isinstance(row, dict) for row in page_rows)
        ):
            raise RuntimeError(f"{SEND_SCAN_RECORD_CALL_ID} 响应结构异常")
        if total != len(page_rows):
            raise RuntimeError(
                f"{SEND_SCAN_RECORD_CALL_ID} 返回 {len(page_rows)}/{total} 条，未能单页取全"
            )
        rows.extend(page_rows)
    return rows


def _sent_today(session: Any, login: dict[str, str], items: list[Dict[str, str]], now: _dt.datetime) -> set[str]:
    """Bills this site already send-scanned today to the same next station.

    Reads the send-scan ledger the caller also verifies against, so a rerun
    after a partial batch does not record the same scan twice.
    """

    stations = {item["bill_code"]: item["station_name"] for item in items}
    today = now.strftime("%Y-%m-%d")
    rows = query_send_scan_records(session, site_code=login["loginSiteCode"], bill_codes=list(stations))
    return {
        code
        for row in rows
        if (code := str(row.get("BILL_CODE") or "").strip()) in stations
        and str(row.get("SCAN_TYPE") or "").strip() == "发件"
        and str(row.get("SCAN_SITE_CODE") or "").strip() == login["loginSiteCode"]
        and str(row.get("PRE_OR_NEXT_STATION") or "").strip() == stations[code]
        and str(row.get("SCAN_DATE") or "").strip().startswith(today)
    }


def _scan_row(
    *,
    index: int,
    bill_code: str,
    station_name: str,
    station_code: str,
    login: dict[str, str],
    now: _dt.datetime,
) -> dict[str, Any]:
    """Build one row exactly as the page's grid sends it to ``saveTables``."""

    timestamp = _json_datetime(now)
    return {
        # ``new mini.Form("#searchForm").getData()`` of the real page
        "SITE_TYPE": "",
        "SCAN_STATUS": 3 if login["loginSiteType"] == CENTER_SITE_TYPE else 2,
        "LONG_TIME": "12",
        "OTHER_BILL": "",
        "SCAN_DATE": timestamp,
        "FAST_TYPE": "汽运",
        # The page's own listing code adds the date parts numerically from
        # October on; keep the 14-digit time code the ledger already holds.
        "LISTING_CODE": now.strftime("%Y%m%d%H%M%S"),
        "BILL_CODE": bill_code,
        "PRE_OR_NEXT_STATION_CODE": station_code,
        "PRE_OR_NEXT_STATION": station_name,
        "REMARK": "",
        "TRANSFERE_BILL_CODE": "",
        "TRUCK_CODE": "",
        # Fields the page's entry handler sets on top of the form
        "SCAN_SITE": login["loginSiteName"],
        "SCAN_SITE_CODE": login["loginSiteCode"],
        "SCAN_MAN": login["loginUserName"],
        "SCAN_MAN_CODE": login["loginUserAccount"],
        "SCAN_TYPE": "发件",
        "DATA_FROM": "K13",
        "DISPATCH_SITE": "",
        "REGISTER_DATE": timestamp,
        # MiniUI bookkeeping carried by every added grid row
        "_id": index,
        "_uid": index,
        "_state": "added",
    }


def _save_scan_rows(session: Any, page: dict[str, str], rows: list[dict[str, Any]]) -> str:
    result = save_tables(session, [operation(SCAN_OPERATION_KEY, rows)], page)
    if not isinstance(result, dict) or result.get("success") is not True:
        message = result.get("message") if isinstance(result, dict) else result
        raise RuntimeError(f"上传失败: {str(message or '')[:300]}")
    return str(result.get("message") or "")


def run_flow(
    *,
    items: Optional[list[Dict[str, str]]] = None,
    session_profile: str = DEFAULT_SESSION_PROFILE,
    station_name: str = DEFAULT_STATION_NAME,
    bill_code: str = DEFAULT_BILL_CODE,
) -> Dict[str, Any]:
    profile = str(session_profile).strip()
    if not profile:
        raise ValueError("session_profile 不能为空")
    started = time.time()
    stage = "init"
    scan_items: list[Dict[str, str]] = []
    station_results: list[Dict[str, Any]] = []
    skipped_signed_codes: list[str] = []
    current_station = ""
    current_bill_code = ""
    try:
        scan_items = _coerce_items(items)
        if not scan_items:
            station_text = str(station_name).strip()
            bill_text = str(bill_code).strip()
            if not station_text or not bill_text:
                raise RuntimeError("未提供扫描数据")
            scan_items = [{"station_name": station_text, "bill_code": bill_text}]
        codes = [item["bill_code"] for item in scan_items]
        if len(set(codes)) != len(codes):
            raise RuntimeError("扫描数据存在重复单号")

        stage = "session"
        session = TMSAuth(profile=profile).login_and_get_session()
        stage = "login_context"
        login = _login_context(session)
        stage = "page_context"
        page = _resolve_menu_page_context(
            session,
            menu_text=SCAN_MENU_TEXT,
            content_markers=(SCAN_OPERATION_KEY,),
        )
        stage = "bill_rules"
        rules = _bill_rules(session, page)
        stage = "sent_today"
        sent_today = _sent_today(session, login, scan_items, _dt.datetime.now(RONGHUI_TIMEZONE))

        row_index = 0
        for station, group in itertools.groupby(scan_items, key=lambda item: item["station_name"]):
            current_station = station
            current_bill_code = ""
            stage = "select_station"
            station_code = _station_code(session, page, station, login["loginSiteType"])
            log(f"下一站: {station} ({station_code})")
            rows: list[dict[str, Any]] = []
            for item in group:
                current_bill_code = item["bill_code"]
                stage = "check_bill"
                if not any(rule.search(current_bill_code) for rule in rules):
                    raise RuntimeError(f"单号不符合融辉单号规则: {current_bill_code}")
                if current_bill_code in sent_today:
                    log(f"单号今日已发件扫描到 {station}，跳过: {current_bill_code}")
                    skipped_signed_codes.append(current_bill_code)
                    continue
                if _is_signed(session, page, current_bill_code):
                    log(f"单号已做过签收，跳过: {current_bill_code}")
                    skipped_signed_codes.append(current_bill_code)
                    continue
                row_index += 1
                rows.append(
                    _scan_row(
                        index=row_index,
                        bill_code=current_bill_code,
                        station_name=station,
                        station_code=station_code,
                        login=login,
                        now=_dt.datetime.now(RONGHUI_TIMEZONE),
                    )
                )
            if not rows:
                continue
            stage = "upload"
            message = _save_scan_rows(session, page, rows)
            log(f"扫描数据上传成功: {station} {len(rows)} 单 {message}".rstrip())
            station_results.append(
                {
                    "station_name": station,
                    "count": len(rows),
                    "bill_codes": [row["BILL_CODE"] for row in rows],
                }
            )

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
                "total_scanned": sum(result["count"] for result in station_results),
                "skipped_signed_codes": list(skipped_signed_codes),
                "skipped_signed_count": len(skipped_signed_codes),
            },
            "ts": _ts(),
            "cost_sec": round(time.time() - started, 3),
        }
    except Exception as exc:
        return {
            "ok": False,
            "stage": stage,
            "message": f"{type(exc).__name__}: {exc}",
            "detail": {
                "station_name": current_station or station_name,
                "bill_code": current_bill_code or bill_code,
                "items": scan_items,
                "stations": station_results,
                "skipped_signed_codes": list(skipped_signed_codes),
                "skipped_signed_count": len(skipped_signed_codes),
            },
            "ts": _ts(),
            "cost_sec": round(time.time() - started, 3),
        }


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Ronghui TMS 发件扫描（页面接口）")
    parser.add_argument("--station-name", default=DEFAULT_STATION_NAME, help="下一站名称")
    parser.add_argument("--bill-code", default=DEFAULT_BILL_CODE, help="扫描单号")
    parser.add_argument("--items-json", default="", help="JSON列表，元素包含站点与单号")
    parser.add_argument("--items-file", default="", help="包含JSON列表的文件路径")
    parser.add_argument(
        "--session-profile",
        default=DEFAULT_SESSION_PROFILE,
        help="共享登录会话配置名",
    )
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
        session_profile=str(args.session_profile),
    )
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get("ok") else 1


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
    session_profile = str(
        _get_param(
            params,
            "session_profile",
            "sessionProfile",
            default=DEFAULT_SESSION_PROFILE,
        )
    ).strip()
    if not session_profile:
        raise ValueError("session_profile 不能为空")

    items = _coerce_items(raw_items)
    if not items:
        items_file = _get_param(params, "items_file", "itemsFile", default="")
        if items_file:
            items = _coerce_items(_load_json_file(str(items_file)))

    return run_flow(
        station_name=station_name,
        bill_code=bill_code,
        items=items,
        session_profile=session_profile,
    )


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
