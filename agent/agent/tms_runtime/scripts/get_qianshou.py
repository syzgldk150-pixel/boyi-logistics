import argparse
import json
import os
from datetime import datetime, timedelta
from math import ceil
from typing import Any, Dict, List, Optional, Tuple

from r13_login_manager import R13SSOAuth


API_URL = "https://r13.ronghuiwl.com/gateway/site/waybillSignWarn/pageGet"
REFERER_URL = "https://r13.ronghuiwl.com/outlets/cargoReceiptWarn"


def _format_dt(value: datetime) -> str:
    return value.strftime("%Y-%m-%d %H:%M:%S")


def _default_range(days: int) -> Tuple[str, str]:
    if days <= 0:
        days = 7
    today = datetime.now()
    end_dt = today.replace(hour=23, minute=59, second=59, microsecond=0)
    start_dt = (end_dt - timedelta(days=days - 1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return _format_dt(start_dt), _format_dt(end_dt)


def _resolve_range(start: Optional[str], end: Optional[str], days: int) -> Tuple[str, str]:
    if start and end:
        return start, end
    return _default_range(days)


def _to_int(value: Any) -> int:
    if value is None:
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _has_value(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return value.strip() != ""
    return True


def _extract_rows(payload: Any) -> List[Dict[str, Any]]:
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        return []

    data = payload.get("data")
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("records", "rows", "list", "items", "data"):
            rows = data.get(key)
            if isinstance(rows, list):
                return rows
        page = data.get("page")
        if isinstance(page, dict):
            for key in ("records", "rows", "list", "items", "data"):
                rows = page.get(key)
                if isinstance(rows, list):
                    return rows
    for key in ("records", "rows", "list", "items", "data"):
        rows = payload.get(key)
        if isinstance(rows, list):
            return rows
    return []


def _extract_total(payload: Any) -> Optional[int]:
    if not isinstance(payload, dict):
        return None
    data = payload.get("data")
    if isinstance(data, dict):
        for key in ("total", "totalCount", "count", "totalNum"):
            value = data.get(key)
            if value is not None:
                return _to_int(value)
        page = data.get("page")
        if isinstance(page, dict):
            for key in ("total", "totalCount", "count", "totalNum"):
                value = page.get(key)
                if value is not None:
                    return _to_int(value)
    for key in ("total", "totalCount", "count", "totalNum"):
        value = payload.get(key)
        if value is not None:
            return _to_int(value)
    return None


def _build_payload(
    *,
    start: str,
    end: str,
    disp_site_code: str,
    page_size: int,
    page: int,
) -> Dict[str, Any]:
    return {
        "queryType": 1,
        "showSub": "10",
        "dispSiteCode_CondList": [disp_site_code],
        "queryDate": [start, end],
        "pageSize": page_size,
        "currentPage": page,
        "queryCount": True,
        "scanTime_CondStart": start,
        "scanTime_CondEnd": end,
    }


def fetch_qianshou(
    *,
    config_path: Optional[str],
    username: Optional[str],
    password: Optional[str],
    account_key: Optional[str] = None,
    disp_site_code: str,
    start: Optional[str],
    end: Optional[str],
    days: int,
    page_size: int,
    page: int,
    fetch_all: bool = True,
    max_pages: int = 500,
) -> List[Dict[str, Any]]:
    auth = R13SSOAuth(config_path=config_path)
    session = auth.login_and_get_session(
        username=username,
        password=password,
        account_key=account_key,
        exchange=False,
        verify=False,
    )
    token = auth.last_token
    if not token:
        raise RuntimeError("Missing aurora token after login.")

    headers = {
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json",
        "aurora-token": token,
        "x-appId": "site",
        "aurora-back": REFERER_URL,
        "Referer": REFERER_URL,
    }

    start_value, end_value = _resolve_range(start, end, days)
    result: List[Dict[str, Any]] = []
    current_page = max(1, page)
    fetched_pages = 0

    while True:
        payload = _build_payload(
            start=start_value,
            end=end_value,
            disp_site_code=disp_site_code,
            page_size=page_size,
            page=current_page,
        )

        response = session.post(API_URL, json=payload, headers=headers, timeout=20)
        response.raise_for_status()
        data = response.json()
        rows = _extract_rows(data)

        for row in rows:
            if not isinstance(row, dict):
                continue
            disp_time = row.get("dispTime")
            if _has_value(disp_time):
                continue
            result.append(
                {
                    "billNumberMain": row.get("billNumberMain"),
                    "planSignTime": row.get("planSignTime"),
                    "goodsName": row.get("goodsName"),
                    "pcs": _to_int(row.get("pcs")),
                    "dispAddress": row.get("dispAddress"),
                    "dispatchMode": row.get("dispatchMode"),
                    "packTypeDesc": row.get("packTypeDesc"),
                }
            )

        fetched_pages += 1
        if not fetch_all:
            break
        if not rows:
            break
        if len(rows) < page_size:
            break
        total = _extract_total(data)
        if total:
            total_pages = max(1, ceil(total / page_size))
            if current_page >= total_pages:
                break
        if fetched_pages >= max_pages:
            break
        current_page += 1

    return result


def _resolve_disp_site_code(value: Optional[str]) -> str:
    if value:
        return value
    return os.environ.get("R13_DISP_SITE_CODE", "7390004")


def _coerce_int(value: Any, *, default: int) -> int:
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _coerce_bool(value: Any, *, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    return default


def run_once(params: Dict[str, Any]) -> List[Dict[str, Any]]:
    disp_site_code = params.get("disp_site_code") or params.get("dispSiteCode")
    if isinstance(disp_site_code, list):
        disp_site_code = disp_site_code[0] if disp_site_code else None

    start = params.get("start")
    end = params.get("end")
    days = _coerce_int(params.get("days"), default=7)
    page_size = _coerce_int(params.get("page_size") or params.get("pageSize"), default=100)
    page = _coerce_int(params.get("page") or params.get("currentPage"), default=1)
    fetch_all = _coerce_bool(params.get("fetch_all") or params.get("fetchAll"), default=True)
    max_pages = _coerce_int(params.get("max_pages") or params.get("maxPages"), default=500)

    return fetch_qianshou(
        config_path=params.get("config_path"),
        username=params.get("username") or params.get("user"),
        password=params.get("password") or params.get("pass"),
        account_key=params.get("account_key") or params.get("accountKey"),
        disp_site_code=_resolve_disp_site_code(disp_site_code),
        start=start,
        end=end,
        days=days,
        page_size=page_size,
        page=page,
        fetch_all=fetch_all,
        max_pages=max_pages,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Fetch r13 arrival pre-report data.")
    parser.add_argument("--config-path", default=None)
    parser.add_argument("--username", default=None)
    parser.add_argument("--password", default=None)
    parser.add_argument("--account-key", default=None)
    parser.add_argument("--disp-site-code", default=None)
    parser.add_argument("--start", default=None, help="YYYY-MM-DD HH:MM:SS")
    parser.add_argument("--end", default=None, help="YYYY-MM-DD HH:MM:SS")
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--page-size", type=int, default=100)
    parser.add_argument("--page", type=int, default=1)
    parser.add_argument("--fetch-all", action="store_true")
    parser.add_argument("--max-pages", type=int, default=500)
    args = parser.parse_args()

    disp_site_code = _resolve_disp_site_code(args.disp_site_code)
    result = fetch_qianshou(
        config_path=args.config_path,
        username=args.username,
        password=args.password,
        account_key=args.account_key,
        disp_site_code=disp_site_code,
        start=args.start,
        end=args.end,
        days=args.days,
        page_size=args.page_size,
        page=args.page,
        fetch_all=args.fetch_all,
        max_pages=args.max_pages,
    )
    print(json.dumps(result, ensure_ascii=True, indent=2))


if __name__ == "__main__":
    main()
