"""
Fetch come-scan records and output BILL_CODE SQL list or JSON rows.

Examples:
  python scripts/get_scan.py
  python scripts/get_scan.py --sql-format values
  python scripts/get_scan.py --sql-format full --table waybill_data --field BILL_CODE
  python scripts/get_scan.py --output-format json
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import logging
import sys
import traceback
from typing import Any, Dict, List, Optional

from agent.tms_runtime.scripts.login_manager import TMSAuth
from agent.tms_runtime.scripts.receipts_sync import (
    _read_user_info_cookie,
    _resolve_login_site_code_from_user_info,
)

SCAN_URL = "https://tms.ronghuiwl.com/dataQuery/findPageByCallId"
CALL_ID = "FIND_COME_SCAN_RECORD"
DATE_FMT = "%Y/%m/%d"
DATE_TIME_FMT = "%Y/%m/%d %H:%M:%S"
DEFAULT_SITE_CODE = "73901"
DEFAULT_SCAN_TYPE = "\u5230\u4ef6"
DEFAULT_PAGE_SIZE = 500
DEFAULT_MAX_PAGES = 500


def parse_date(value: str) -> _dt.date:
    try:
        return _dt.datetime.strptime(value, DATE_FMT).date()
    except ValueError as exc:
        raise argparse.ArgumentTypeError("Date must be in YYYY/MM/DD format.") from exc


def parse_datetime(value: str) -> _dt.datetime:
    try:
        return _dt.datetime.strptime(value, DATE_TIME_FMT)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("Datetime must be in YYYY/MM/DD HH:MM:SS format.") from exc


def configure_logging(debug: bool) -> None:
    level = logging.DEBUG if debug else logging.ERROR
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )


def _module_available(name: str) -> bool:
    try:
        __import__(name)
        return True
    except Exception:
        return False


def build_accept_encoding() -> str:
    encodings = ["gzip", "deflate"]
    if _module_available("brotli") or _module_available("brotlicffi"):
        encodings.append("br")
    if _module_available("zstandard"):
        encodings.append("zstd")
    return ", ".join(encodings)


def build_headers() -> Dict[str, str]:
    return {
        "Accept": "text/plain, */*; q=0.01",
        "Accept-Encoding": build_accept_encoding(),
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Connection": "keep-alive",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "Origin": "https://tms.ronghuiwl.com",
        "Referer": "https://tms.ronghuiwl.com/widget/home",
        "X-Requested-With": "XMLHttpRequest",
    }


def resolve_base_date(
    target_date: Optional[_dt.date],
    start_dt: Optional[_dt.datetime],
    end_dt: Optional[_dt.datetime],
) -> _dt.date:
    if target_date is not None:
        return target_date
    if start_dt is not None:
        return start_dt.date()
    if end_dt is not None:
        return end_dt.date()
    return _dt.date.today()


def build_date_range(
    base_date: _dt.date,
    start_dt: Optional[_dt.datetime],
    end_dt: Optional[_dt.datetime],
) -> Dict[str, str]:
    if start_dt is None:
        start_dt = _dt.datetime.combine(base_date, _dt.time(0, 0, 0))
    if end_dt is None:
        end_dt = _dt.datetime.combine(base_date, _dt.time(23, 59, 59))
    if start_dt > end_dt:
        raise ValueError("start must be <= end")
    return {
        "start": start_dt.strftime(DATE_TIME_FMT),
        "end": end_dt.strftime(DATE_TIME_FMT),
    }


def build_payload(
    date_range_json: str,
    site_code: str,
    scan_type: str,
    page_index: int,
    page_size: int,
) -> Dict[str, str]:
    return {
        "searchOrderType": "BILL_CODE",
        "searchOrderInput": "",
        "SCAN_TYPE": scan_type,
        "searchDateType": "SCAN_DATE",
        "SEARCH_DATE_RANGE": date_range_json,
        "SCAN_DATE": date_range_json,
        "SCAN_SITE_CODE": site_code,
        "LOGIN_SITE_CODE": site_code,
        "pageIndex": str(page_index),
        "pageSize": str(page_size),
        "REMARK": "",
        "BL_SUB_RECEIPT": "",
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
        SCAN_URL,
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


def extract_data_list(payload: Any) -> List[Dict[str, Any]]:
    if not isinstance(payload, dict):
        raise ValueError("TMS scan response must be an object")
    if "data" in payload:
        data = payload["data"]
    else:
        result = payload.get("result")
        if not isinstance(result, dict) or "data" not in result:
            raise ValueError("TMS scan response is missing the data list")
        data = result["data"]
    if not isinstance(data, list):
        raise ValueError("TMS scan response data must be a list")
    if any(not isinstance(item, dict) for item in data):
        raise ValueError("TMS scan response contains an invalid row")
    return list(data)


def normalize_scan_row(item: Dict[str, Any]) -> Optional[Dict[str, str]]:
    if not isinstance(item, dict):
        return None
    code = item.get("BILL_CODE")
    if code is None:
        return None
    code_text = str(code).strip()
    if not code_text:
        return None
    destination = item.get("DESTINATION")
    destination_text = str(destination).strip() if destination is not None else ""
    scan_type = str(item.get("SCAN_TYPE") or "").strip()
    scan_time = str(item.get("SCAN_DATE") or item.get("REGISTER_DATE") or "").strip()
    scan_site = str(item.get("SCAN_SITE") or "").strip()
    return {
        "bill_code": code_text,
        "destination": destination_text,
        "scan_type": scan_type,
        "scan_time": scan_time,
        "scan_site": scan_site,
    }


def collect_scan_rows(
    session: Any,
    date_range: Dict[str, str],
    site_code: str,
    scan_type: str,
    page_size: int,
    timeout: float,
    max_pages: int = DEFAULT_MAX_PAGES,
) -> List[Dict[str, str]]:
    logger = logging.getLogger(__name__)
    headers = build_headers()
    date_range_json = json.dumps(date_range, ensure_ascii=False)

    seen: set[tuple[str, str, str, str]] = set()
    scan_identity_rows: dict[tuple[str, str, str], Dict[str, str]] = {}
    rows: List[Dict[str, str]] = []
    page_index = 0

    while True:
        payload = build_payload(date_range_json, site_code, scan_type, page_index, page_size)
        raw = fetch_page(session, payload, headers, timeout, page_index)
        if not isinstance(raw, dict):
            raise RuntimeError(f"TMS scan page {page_index} returned a non-object response")
        raw_data = raw.get("data")
        if raw_data is None and isinstance(raw.get("result"), dict):
            raw_data = raw["result"].get("data")
        if not isinstance(raw_data, list):
            raise RuntimeError(f"TMS scan page {page_index} response is missing the data list")
        items = extract_data_list(raw)
        if not items:
            logger.debug("No data on page %s; stop.", page_index)
            break

        added = 0
        for item in items:
            row = normalize_scan_row(item)
            if row is None:
                raise RuntimeError(f"TMS scan page {page_index} contains a row without BILL_CODE")
            scan_identity = (row["bill_code"], row.get("scan_type", ""), row.get("scan_time", ""))
            previous = scan_identity_rows.get(scan_identity)
            if previous is not None and previous != row:
                raise RuntimeError(
                    "TMS scan duplicate has conflicting data: "
                    f"{row['bill_code']} {row.get('scan_type', '')} {row.get('scan_time', '')}"
                )
            scan_identity_rows[scan_identity] = row
            identity = (
                row["bill_code"],
                row.get("scan_type", ""),
                row.get("scan_time", ""),
                row.get("scan_site", ""),
            )
            if identity in seen:
                continue
            seen.add(identity)
            rows.append(row)
            added += 1

        logger.debug(
            "Fetched page %s: items=%s new_rows=%s total=%s",
            page_index,
            len(items),
            added,
            len(rows),
        )
        page_index += 1
        if page_index >= max_pages:
            raise RuntimeError(
                f"TMS scan pagination reached max_pages={max_pages} before an empty terminal page"
            )

    return rows


def collect_bill_codes(
    session: Any,
    date_range: Dict[str, str],
    site_code: str,
    scan_type: str,
    page_size: int,
    timeout: float,
) -> List[str]:
    rows = collect_scan_rows(
        session,
        date_range,
        site_code=site_code,
        scan_type=scan_type,
        page_size=page_size,
        timeout=timeout,
    )
    return [row["bill_code"] for row in rows]


def format_scan_rows(rows: List[Dict[str, str]]) -> List[Dict[str, str]]:
    formatted: List[Dict[str, str]] = []
    for row in rows:
        formatted.append(
            {
                "扫描单号": row["bill_code"],
                "目的地": row["destination"],
                "扫描类型": row.get("scan_type", ""),
                "扫描时间": row.get("scan_time", ""),
                "扫描网点": row.get("scan_site", ""),
            }
        )
    return formatted


def escape_sql_literal(value: str) -> str:
    return value.replace("'", "''")


def format_in_list(values: List[str]) -> str:
    if not values:
        return "(NULL)"
    literals = [f"'{escape_sql_literal(value)}'" for value in values]
    return f"({','.join(literals)})"


def format_values(values: List[str]) -> str:
    if not values:
        return "(NULL)"
    literals = [f"'{escape_sql_literal(value)}'" for value in values]
    return ",".join(f"({literal})" for literal in literals)


def format_sql(values: List[str], sql_format: str, table: str, field: str) -> str:
    if sql_format == "in_list":
        return format_in_list(values)
    if sql_format == "values":
        return format_values(values)
    if sql_format == "full":
        if not table:
            raise ValueError("--sql-format full requires --table")
        field_name = field or "BILL_CODE"
        return f"SELECT * FROM {table} WHERE {field_name} IN {format_in_list(values)};"
    raise ValueError(f"Unsupported sql format: {sql_format}")


def write_output(sql_text: str, out_path: str) -> None:
    with open(out_path, "w", encoding="utf-8") as handle:
        handle.write(sql_text)
        handle.write("\n")


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Fetch come-scan records and output BILL_CODE SQL list or JSON rows.",
    )
    parser.add_argument("--date", type=parse_date, default=None, help="Date in YYYY/MM/DD (default: today).")
    parser.add_argument("--start", type=parse_datetime, default=None, help="Start datetime in YYYY/MM/DD HH:MM:SS.")
    parser.add_argument("--end", type=parse_datetime, default=None, help="End datetime in YYYY/MM/DD HH:MM:SS.")
    parser.add_argument("--site-code", default=DEFAULT_SITE_CODE, help="Site code for SCAN_SITE_CODE/LOGIN_SITE_CODE.")
    parser.add_argument("--scan-type", default=DEFAULT_SCAN_TYPE, help="SCAN_TYPE value.")
    parser.add_argument("--page-size", type=int, default=DEFAULT_PAGE_SIZE, help="Page size (default: 500).")
    parser.add_argument("--max-pages", type=int, default=DEFAULT_MAX_PAGES, help="Pagination safety limit.")
    parser.add_argument("--timeout", type=float, default=20, help="Request timeout in seconds.")
    parser.add_argument("--out", default="", help="Optional output file path.")
    parser.add_argument(
        "--output-format",
        choices=["sql", "json"],
        default="sql",
        help="Output format: sql (default) or json (BILL_CODE + DESTINATION).",
    )
    parser.add_argument(
        "--sql-format",
        choices=["in_list", "values", "full"],
        default="in_list",
        help="Output SQL format (when output-format=sql).",
    )
    parser.add_argument("--table", default="", help="Table name for sql-format=full.")
    parser.add_argument("--field", default="BILL_CODE", help="Field name for sql-format=full.")
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

        output_format = str(args.output_format).strip().lower()
        if output_format not in {"sql", "json"}:
            raise ValueError(f"Unsupported output format: {output_format}")

        table = ""
        field = "BILL_CODE"
        sql_format = ""
        if output_format == "sql":
            sql_format = str(args.sql_format)
            if sql_format not in {"in_list", "values", "full"}:
                raise ValueError(f"Unsupported sql format: {sql_format}")
            table = str(args.table).strip()
            field = str(args.field).strip() if args.field else "BILL_CODE"
            if sql_format == "full" and not table:
                raise ValueError("--sql-format full requires --table")

        base_date = resolve_base_date(args.date, args.start, args.end)
        date_range = build_date_range(base_date, args.start, args.end)

        auth = TMSAuth()
        session = auth.login_and_get_session()
        if session is None:
            raise RuntimeError("Login failed; session is None")

        if output_format == "json":
            rows = collect_scan_rows(
                session,
                date_range,
                site_code=str(args.site_code),
                scan_type=str(args.scan_type),
                page_size=page_size,
                timeout=timeout,
                max_pages=int(args.max_pages),
            )
            output_rows = format_scan_rows(rows)
            output_text = json.dumps(output_rows, ensure_ascii=False, indent=2)
        else:
            codes = collect_bill_codes(
                session,
                date_range,
                site_code=str(args.site_code),
                scan_type=str(args.scan_type),
                page_size=page_size,
                timeout=timeout,
            )
            output_text = format_sql(codes, sql_format, table, field)

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
    max_pages = int(_get_param(params, "max_pages", "maxPages", default=DEFAULT_MAX_PAGES))
    timeout = float(_get_param(params, "timeout", default=20))
    if page_size <= 0:
        raise ValueError("page-size must be > 0")
    if max_pages <= 0:
        raise ValueError("max-pages must be > 0")
    if timeout <= 0:
        raise ValueError("timeout must be > 0")

    output_format = str(_get_param(params, "output_format", "outputFormat", default="sql")).strip().lower()
    if output_format not in {"sql", "json"}:
        raise ValueError(f"Unsupported output format: {output_format}")

    sql_format = ""
    table = ""
    field = "BILL_CODE"
    if output_format == "sql":
        sql_format = str(_get_param(params, "sql_format", "sqlFormat", default="in_list"))
        if sql_format not in {"in_list", "values", "full"}:
            raise ValueError(f"Unsupported sql format: {sql_format}")

        table = str(_get_param(params, "table", default="")).strip()
        field = str(_get_param(params, "field", default="BILL_CODE")).strip()
        if sql_format == "full" and not table:
            raise ValueError("--sql-format full requires --table")

    base_date = resolve_base_date(target_date, start_dt, end_dt)
    date_range = build_date_range(base_date, start_dt, end_dt)

    explicit_site_code = _get_param(params, "site_code", "siteCode")
    scan_type = str(_get_param(params, "scan_type", "scanType", default=DEFAULT_SCAN_TYPE))

    session_profile = str(_get_param(params, "session_profile", default="default"))
    auth = TMSAuth(profile=session_profile)
    session = auth.login_and_get_session()
    if session is None:
        raise RuntimeError("Login failed; session is None")
    if _coerce_bool(_get_param(params, "use_login_site_code", default=False)):
        site_code = str(explicit_site_code or "").strip()
        if not site_code:
            site_code = _resolve_login_site_code_from_user_info(_read_user_info_cookie(session))
        if not site_code:
            raise RuntimeError("TMS 登录态缺少 loginSiteCode，无法准确查询本站签收扫描")
    else:
        site_code = str(explicit_site_code or DEFAULT_SITE_CODE)

    if output_format == "json":
        rows = collect_scan_rows(
            session,
            date_range,
            site_code=site_code,
            scan_type=scan_type,
            page_size=page_size,
            timeout=timeout,
            max_pages=max_pages,
        )
        output_rows = format_scan_rows(rows)
        output_text = json.dumps(output_rows, ensure_ascii=False, indent=2)
        output_data: Any = output_rows
    else:
        codes = collect_bill_codes(
            session,
            date_range,
            site_code=site_code,
            scan_type=scan_type,
            page_size=page_size,
            timeout=timeout,
        )
        output_text = format_sql(codes, sql_format, table, field)
        output_data = output_text

    out_path = _get_param(params, "out", default="")
    if out_path:
        write_output(output_text, str(out_path))

    return output_data


if __name__ == "__main__":
    raise SystemExit(main())
