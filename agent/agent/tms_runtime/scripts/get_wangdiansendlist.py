"""
Fetch scan receive list data and return selected fields.

Uses login_manager.TMSAuth for authentication.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import sys
import traceback
from typing import Any, Dict, List, Optional

from get_infor import fetch_bill_info_html, parse_bill_info_html
from agent.tms_runtime.scripts.login_manager import TMSAuth

DATA_QUERY_URL = "https://tms.ronghuiwl.com/dataQuery/findPageByCallId"
CALL_ID = "FIND_SCAN_REC_LIST"
DATE_FMT = "%Y/%m/%d"
DATE_TIME_FMT = "%Y/%m/%d %H:%M:%S"
DEFAULT_SITE_CODE = "73901"
DEFAULT_PAGE_SIZE = 100

LABEL_SEND_SITE = "\u53d1\u8d27\u7f51\u70b9"
LABEL_DESTINATION = "\u76ee\u7684\u7f51\u70b9"
LABEL_BILL_CODE = "\u8fd0\u5355\u7f16\u53f7"
LABEL_PIECE_NUMBER = "\u4ef6\u6570"
LABEL_BILL_WEIGHT = "\u91cd\u91cf"
LABEL_PACK_TYPE = "\u5305\u88c5\u7c7b\u578b"

EXCLUDED_SEND_SITES = {
    "\u90b5\u9633\u65b0\u90b5\u7ad9",
    "\u90b5\u9633\u5927\u7965\u7ad9",
    "\u90b5\u9633\u9e4f\u8fbe\u8425\u4e1a\u90e8",
}
EXCLUDED_BILL_PREFIXES = ("HR", "H")


def parse_date(value: str) -> dt.date:
    try:
        return dt.datetime.strptime(value, DATE_FMT).date()
    except ValueError as exc:
        raise argparse.ArgumentTypeError("Date must be in YYYY/MM/DD format.") from exc


def parse_datetime(value: str) -> dt.datetime:
    try:
        return dt.datetime.strptime(value, DATE_TIME_FMT)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("Datetime must be in YYYY/MM/DD HH:MM:SS format.") from exc


def configure_logging(debug: bool) -> None:
    level = logging.DEBUG if debug else logging.ERROR
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )


def build_headers() -> Dict[str, str]:
    return {
        "Accept": "text/plain, */*; q=0.01",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "Origin": "https://tms.ronghuiwl.com",
        "Referer": "https://tms.ronghuiwl.com/widget/home",
        "X-Requested-With": "XMLHttpRequest",
    }


def resolve_base_date(
    target_date: Optional[dt.date],
    start_dt: Optional[dt.datetime],
    end_dt: Optional[dt.datetime],
) -> dt.date:
    if target_date is not None:
        return target_date
    if start_dt is not None:
        return start_dt.date()
    if end_dt is not None:
        return end_dt.date()
    return dt.date.today()


def build_date_range(
    base_date: dt.date,
    start_dt: Optional[dt.datetime],
    end_dt: Optional[dt.datetime],
) -> Dict[str, str]:
    if start_dt is None:
        start_dt = dt.datetime.combine(base_date, dt.time(0, 0, 0))
    if end_dt is None:
        end_dt = dt.datetime.combine(base_date, dt.time(23, 59, 59))
    if start_dt > end_dt:
        raise ValueError("start must be <= end")
    return {
        "start": start_dt.strftime(DATE_TIME_FMT),
        "end": end_dt.strftime(DATE_TIME_FMT),
    }


def build_payload(
    date_range_json: str,
    login_site_code: str,
    page_index: int,
    page_size: int,
    *,
    search_order_type: str = "BILL_CODE",
    search_order_input: str = "",
    scan_site_code: str = "",
    pre_or_next_station_code: str = "",
    fast_type: str = "",
    bl_sub_bill: str = "",
    sum_pic: str = "",
    sum_w: str = "",
    sum_sub_w: str = "",
) -> Dict[str, str]:
    return {
        "searchOrderType": search_order_type,
        "searchOrderInput": search_order_input,
        "searchDateType": "SCAN_DATE",
        "SEARCH_DATE_RANGE": date_range_json,
        "SUM_PIC": sum_pic,
        "SCAN_SITE_CODE": scan_site_code,
        "PRE_OR_NEXT_STATION_CODE": pre_or_next_station_code,
        "SUM_W": sum_w,
        "FAST_TYPE": fast_type,
        "BL_SUB_BILL": bl_sub_bill,
        "SUM_SUB_W": sum_sub_w,
        "SCAN_DATE": date_range_json,
        "LOGIN_SITE_CODE": login_site_code,
        "pageIndex": str(page_index),
        "pageSize": str(page_size),
        "sortField": "",
        "sortOrder": "",
        "totalColumns": "[]",
    }


def safe_response_snippet(resp: Any, limit: int = 500) -> str:
    try:
        text = resp.text
    except Exception:
        text = None
    if text:
        return text[:limit]
    try:
        return repr(resp.content[:limit])
    except Exception:
        return ""


def fetch_page(
    session: Any,
    payload: Dict[str, str],
    headers: Dict[str, str],
    timeout: float,
    page_index: int,
) -> Any:
    resp = session.post(
        DATA_QUERY_URL,
        params={"id": CALL_ID},
        data=payload,
        headers=headers,
        allow_redirects=False,
        timeout=timeout,
    )
    if resp.status_code != 200:
        snippet = safe_response_snippet(resp)
        print(
            f"HTTP {resp.status_code} on page {page_index}. Response snippet: {snippet}",
            file=sys.stderr,
        )
        resp.raise_for_status()
    try:
        return resp.json()
    except Exception as exc:
        snippet = safe_response_snippet(resp)
        print(
            f"JSON parse failed on page {page_index}: {exc}. Response snippet: {snippet}",
            file=sys.stderr,
        )
        raise


def extract_rows(payload: Any) -> List[Dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    data = payload.get("data")
    if data is None:
        result = payload.get("result") or {}
        if isinstance(result, dict):
            data = result.get("data")
    if not isinstance(data, list):
        return []
    return [item for item in data if isinstance(item, dict)]


def extract_total(payload: Any) -> Optional[int]:
    if not isinstance(payload, dict):
        return None
    for key in ("total", "totalCount", "count"):
        value = payload.get(key)
        if value is not None:
            try:
                return int(value)
            except (TypeError, ValueError):
                return None
    result = payload.get("result")
    if isinstance(result, dict):
        for key in ("total", "totalCount", "count"):
            value = result.get(key)
            if value is not None:
                try:
                    return int(value)
                except (TypeError, ValueError):
                    return None
    return None


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _has_value(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return value.strip() != ""
    return True


def _is_excluded_bill_code(bill_code: str) -> bool:
    code = bill_code.strip().upper()
    return any(code.startswith(prefix) for prefix in EXCLUDED_BILL_PREFIXES)


def _is_excluded_send_site(send_site: str) -> bool:
    return send_site in EXCLUDED_SEND_SITES


def _fetch_pack_type(
    session: Any,
    bill_code: str,
    cache: Dict[str, str],
    *,
    timeout: float,
    is_encryption: bool = True,
) -> str:
    cached = cache.get(bill_code)
    if cached is not None:
        return cached
    logger = logging.getLogger(__name__)
    try:
        html = fetch_bill_info_html(
            session,
            bill_code,
            is_encryption=is_encryption,
            timeout=timeout,
        )
        fields = parse_bill_info_html(html)
        pack_type = str(fields.get(LABEL_PACK_TYPE, "") or "").strip()
    except Exception as exc:
        logger.debug("Pack type fetch failed for %s: %s", bill_code, exc)
        pack_type = ""
    cache[bill_code] = pack_type
    return pack_type


def normalize_row(item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    send_site = _as_text(item.get("SCAN_SITE") or item.get("SCAN_SITE_NAME"))
    destination = _as_text(item.get("DESTINATION") or item.get("DESTINATION_NAME"))
    bill_code = _as_text(item.get("BILL_CODE"))
    if not bill_code:
        return None
    if _is_excluded_bill_code(bill_code):
        return None
    if _is_excluded_send_site(send_site):
        return None

    piece_number = item.get("PIECE_NUMBER")
    bill_weight = item.get("SETTLEMENT_WEIGHT")
    if not any(
        [
            send_site,
            destination,
            _has_value(piece_number),
            _has_value(bill_weight),
        ]
    ):
        return None
    return {
        LABEL_SEND_SITE: send_site,
        LABEL_DESTINATION: destination,
        LABEL_BILL_CODE: bill_code,
        LABEL_PIECE_NUMBER: "" if piece_number is None else piece_number,
        LABEL_BILL_WEIGHT: "" if bill_weight is None else bill_weight,
    }


def collect_rows(
    session: Any,
    date_range: Dict[str, str],
    site_code: str,
    page_size: int,
    timeout: float,
    *,
    page_index: int = 0,
    fetch_all: bool = False,
    search_order_type: str = "BILL_CODE",
    search_order_input: str = "",
    scan_site_code: str = "",
    pre_or_next_station_code: str = "",
    fast_type: str = "",
    bl_sub_bill: str = "",
    sum_pic: str = "",
    sum_w: str = "",
    sum_sub_w: str = "",
) -> List[Dict[str, Any]]:
    headers = build_headers()
    date_range_json = json.dumps(date_range, ensure_ascii=False)
    results: List[Dict[str, Any]] = []
    current_page = page_index
    pack_type_cache: Dict[str, str] = {}

    while True:
        payload = build_payload(
            date_range_json,
            site_code,
            current_page,
            page_size,
            search_order_type=search_order_type,
            search_order_input=search_order_input,
            scan_site_code=scan_site_code,
            pre_or_next_station_code=pre_or_next_station_code,
            fast_type=fast_type,
            bl_sub_bill=bl_sub_bill,
            sum_pic=sum_pic,
            sum_w=sum_w,
            sum_sub_w=sum_sub_w,
        )
        raw = fetch_page(session, payload, headers, timeout, current_page)
        items = extract_rows(raw)
        if not items:
            break

        for item in items:
            row = normalize_row(item)
            if row is not None:
                bill_code = row.get(LABEL_BILL_CODE, "")
                if bill_code:
                    row[LABEL_PACK_TYPE] = _fetch_pack_type(
                        session,
                        str(bill_code),
                        pack_type_cache,
                        timeout=timeout,
                    )
                else:
                    row[LABEL_PACK_TYPE] = ""
                results.append(row)

        if not fetch_all:
            break
        total = extract_total(raw)
        if total is not None and (current_page + 1) * page_size >= total:
            break
        if len(items) < page_size:
            break
        current_page += 1

    return results


def write_output(text: str, out_path: str) -> None:
    with open(out_path, "w", encoding="utf-8") as handle:
        handle.write(text)
        handle.write("\n")


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fetch scan receive list data.")
    parser.add_argument("--date", type=parse_date, default=None, help="Date in YYYY/MM/DD (default: today).")
    parser.add_argument("--start", type=parse_datetime, default=None, help="Start datetime in YYYY/MM/DD HH:MM:SS.")
    parser.add_argument("--end", type=parse_datetime, default=None, help="End datetime in YYYY/MM/DD HH:MM:SS.")
    parser.add_argument("--site-code", default=DEFAULT_SITE_CODE, help="LOGIN_SITE_CODE value.")
    parser.add_argument("--page-index", type=int, default=0, help="Page index (0-based).")
    parser.add_argument("--page-size", type=int, default=DEFAULT_PAGE_SIZE, help="Page size (default: 100).")
    parser.add_argument("--fetch-all", action="store_true", help="Fetch all pages.")
    parser.add_argument("--timeout", type=float, default=20, help="Request timeout in seconds.")
    parser.add_argument("--out", default="", help="Optional output file path.")
    parser.add_argument("--debug", action="store_true", help="Enable debug logging to stderr.")
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    configure_logging(bool(args.debug))

    try:
        page_size = int(args.page_size)
        if page_size <= 0:
            raise ValueError("page-size must be > 0")
        timeout = float(args.timeout)
        if timeout <= 0:
            raise ValueError("timeout must be > 0")

        base_date = resolve_base_date(args.date, args.start, args.end)
        date_range = build_date_range(base_date, args.start, args.end)

        auth = TMSAuth()
        session = auth.login_and_get_session()
        if session is None:
            raise RuntimeError("Login failed; session is None")

        rows = collect_rows(
            session,
            date_range,
            site_code=str(args.site_code),
            page_size=page_size,
            timeout=timeout,
            page_index=int(args.page_index),
            fetch_all=bool(args.fetch_all),
        )

        output_text = json.dumps(rows, ensure_ascii=False, indent=2)
        if args.out:
            write_output(output_text, str(args.out))

        print(output_text)
        return 0
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        if args.debug:
            traceback.print_exc()
        return 1


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
    debug = _coerce_bool(_get_param(params, "debug", default=False))
    if debug:
        logging.getLogger(__name__).setLevel(logging.DEBUG)

    date_value = _get_param(params, "date")
    start_value = _get_param(params, "start")
    end_value = _get_param(params, "end")

    target_date = parse_date(str(date_value)) if date_value else None
    start_dt = parse_datetime(str(start_value)) if start_value else None
    end_dt = parse_datetime(str(end_value)) if end_value else None

    page_size = int(_get_param(params, "page_size", "pageSize", default=DEFAULT_PAGE_SIZE))
    page_index = int(_get_param(params, "page_index", "pageIndex", default=0))
    timeout = float(_get_param(params, "timeout", default=20))
    fetch_all = _coerce_bool(_get_param(params, "fetch_all", "fetchAll", default=False))
    if page_size <= 0:
        raise ValueError("page-size must be > 0")
    if timeout <= 0:
        raise ValueError("timeout must be > 0")

    base_date = resolve_base_date(target_date, start_dt, end_dt)
    date_range = build_date_range(base_date, start_dt, end_dt)

    site_code = str(_get_param(params, "site_code", "siteCode", default=DEFAULT_SITE_CODE))
    search_order_type = str(_get_param(params, "search_order_type", "searchOrderType", default="BILL_CODE"))
    search_order_input = str(_get_param(params, "search_order_input", "searchOrderInput", default=""))
    scan_site_code = str(_get_param(params, "scan_site_code", "scanSiteCode", default=""))
    pre_or_next_station_code = str(
        _get_param(params, "pre_or_next_station_code", "preOrNextStationCode", default="")
    )
    fast_type = str(_get_param(params, "fast_type", "fastType", default=""))
    bl_sub_bill = str(_get_param(params, "bl_sub_bill", "blSubBill", default=""))
    sum_pic = str(_get_param(params, "sum_pic", "sumPic", default=""))
    sum_w = str(_get_param(params, "sum_w", "sumW", default=""))
    sum_sub_w = str(_get_param(params, "sum_sub_w", "sumSubW", default=""))

    auth = TMSAuth()
    session = auth.login_and_get_session()
    if session is None:
        raise RuntimeError("Login failed; session is None")

    rows = collect_rows(
        session,
        date_range,
        site_code=site_code,
        page_size=page_size,
        timeout=timeout,
        page_index=page_index,
        fetch_all=fetch_all,
        search_order_type=search_order_type,
        search_order_input=search_order_input,
        scan_site_code=scan_site_code,
        pre_or_next_station_code=pre_or_next_station_code,
        fast_type=fast_type,
        bl_sub_bill=bl_sub_bill,
        sum_pic=sum_pic,
        sum_w=sum_w,
        sum_sub_w=sum_sub_w,
    )

    out_path = _get_param(params, "out", default="")
    if out_path:
        output_text = json.dumps(rows, ensure_ascii=False, indent=2)
        write_output(output_text, str(out_path))

    return rows


if __name__ == "__main__":
    raise SystemExit(main())
