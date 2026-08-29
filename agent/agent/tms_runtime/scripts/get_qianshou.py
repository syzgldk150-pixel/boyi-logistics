import argparse
import json
from datetime import datetime, timedelta
from math import ceil
from typing import Any, Dict, List, Optional, Tuple

from agent.tms_runtime.scripts.r13_login_manager import R13SSOAuth
from agent.tms_runtime.sso_session_persistence import default_sso_state_path


API_URL = "https://r13.ronghuiwl.com/gateway/site/waybillSignWarn/pageGet"
AUTH_CONTEXT_URL = "https://r13.ronghuiwl.com/gateway/site/public/aurora/auth"
REFERER_URL = "https://r13.ronghuiwl.com/outlets/cargoReceiptWarn"
R13_PAGE_MIN_LOOKBACK_DAYS = 2
R13_PAGE_FORWARD_DAYS = 3


def _format_dt(value: datetime) -> str:
    return value.strftime("%Y-%m-%d %H:%M:%S")


def _default_range(days: int) -> Tuple[str, str]:
    if days <= 0:
        days = 7
    today = datetime.now()
    start_dt = (
        today - timedelta(days=max(days - 1, R13_PAGE_MIN_LOOKBACK_DAYS))
    ).replace(hour=0, minute=0, second=0, microsecond=0)
    end_dt = (today + timedelta(days=R13_PAGE_FORWARD_DAYS)).replace(
        hour=23,
        minute=59,
        second=59,
        microsecond=0,
    )
    return _format_dt(start_dt), _format_dt(end_dt)


def _resolve_range(start: Optional[str], end: Optional[str], days: int) -> Tuple[str, str]:
    if start and end:
        return start, end
    return _default_range(days)


def _optional_nonnegative_int(value: Any, *, field: str) -> int | None:
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        raise RuntimeError(f"R13 {field} must be a non-negative integer")
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"R13 {field} must be a non-negative integer") from exc
    if parsed < 0:
        raise RuntimeError(f"R13 {field} must be a non-negative integer")
    return parsed


def _has_value(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return value.strip() != ""
    return True


def _has_confirmed_sign_signal(row: Dict[str, Any]) -> bool:
    """Detect an R13 sign signal for diagnostics only.

    The authoritative ledger never uses this signal to close a waybill. Only
    a verified TMS main-waybill sign event can close it.
    """

    return _has_value(row.get("signTime")) or _has_value(row.get("signSiteName"))


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


def _has_rows_container(payload: Any) -> bool:
    if isinstance(payload, list):
        return True
    if not isinstance(payload, dict):
        return False
    candidates = [payload]
    data = payload.get("data")
    if isinstance(data, dict):
        candidates.append(data)
        if isinstance(data.get("page"), dict):
            candidates.append(data["page"])
    return any(
        isinstance(candidate.get(key), list)
        for candidate in candidates
        for key in ("records", "rows", "list", "items", "data")
    )


def _is_invalid_token_response(payload: Any) -> bool:
    if not isinstance(payload, dict) or str(payload.get("code")) != "-2":
        return False
    message = str(payload.get("message") or payload.get("msg") or "").strip().lower()
    return "token" in message


def _require_success_response(payload: Any, *, label: str) -> None:
    if not isinstance(payload, dict) or "code" not in payload:
        return
    if str(payload.get("code")) not in {"0", "1", "200"}:
        raise RuntimeError(f"{label} returned an explicit failure code")


def _read_account_site_filter(payload: Any) -> List[str]:
    if not isinstance(payload, dict) or str(payload.get("code")) not in {"0", "1", "200"}:
        raise RuntimeError("R13 account context request did not succeed")
    context = payload.get("data")
    if not isinstance(context, dict):
        raise RuntimeError("R13 account context is missing")
    raw_site_type_code = context.get("siteTypeCode")
    if raw_site_type_code in (None, ""):
        raise RuntimeError("R13 account context is missing siteTypeCode")
    try:
        site_type_code = int(str(raw_site_type_code).strip())
    except ValueError as exc:
        raise RuntimeError("R13 account context has an invalid site type") from exc
    if site_type_code == 999:
        return []
    site_code = str(context.get("siteCode") or "").strip()
    if not site_code:
        raise RuntimeError("R13 account context is missing siteCode")
    return [site_code]


def _request_account_context(session: Any, headers: Dict[str, str]) -> Any:
    response = session.post(
        AUTH_CONTEXT_URL,
        json={},
        headers=headers,
        timeout=20,
    )
    response.raise_for_status()
    return response.json()


def _extract_total(payload: Any) -> Optional[int]:
    if not isinstance(payload, dict):
        return None
    data = payload.get("data")
    if isinstance(data, dict):
        for key in ("total", "totalCount", "count", "totalNum"):
            value = data.get(key)
            if value is not None:
                return _optional_nonnegative_int(value, field=key)
        page = data.get("page")
        if isinstance(page, dict):
            for key in ("total", "totalCount", "count", "totalNum"):
                value = page.get(key)
                if value is not None:
                    return _optional_nonnegative_int(value, field=key)
    for key in ("total", "totalCount", "count", "totalNum"):
        value = payload.get(key)
        if value is not None:
            return _optional_nonnegative_int(value, field=key)
    return None


def _build_payload(
    *,
    start: str,
    end: str,
    disp_site_codes: List[str],
    page_size: int,
    page: int,
) -> Dict[str, Any]:
    return {
        "queryType": 2,
        "showSub": "10",
        "dispSiteCode_CondList": list(disp_site_codes),
        "queryDate": [start, end],
        "pageSize": page_size,
        "currentPage": page,
        "queryCount": True,
        "planSignTime_CondStart": start,
        "planSignTime_CondEnd": end,
    }


def fetch_qianshou(
    *,
    config_path: Optional[str],
    username: Optional[str],
    password: Optional[str],
    account_key: Optional[str] = None,
    start: Optional[str],
    end: Optional[str],
    days: int,
    page_size: int,
    page: int,
    fetch_all: bool = True,
    max_pages: int = 500,
    account_id: str,
) -> List[Dict[str, Any]]:
    resolved_account_id = str(account_id or "").strip()
    if not resolved_account_id:
        raise RuntimeError("R13 account identity is required")
    auth = R13SSOAuth(
        config_path=config_path,
        state_path=default_sso_state_path(resolved_account_id),
    )
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
    seen_rows: Dict[str, Dict[str, Any]] = {}
    expected_total: int | None = None
    fetched_source_rows = 0
    current_page = max(1, page)
    fetched_pages = 0
    token_refreshed = False

    account_context = _request_account_context(session, headers)
    if _is_invalid_token_response(account_context):
        session = auth.login_and_get_session(
            username=username,
            password=password,
            account_key=account_key,
            exchange=False,
            verify=False,
            allow_cached=False,
        )
        token = auth.last_token
        if not token:
            raise RuntimeError("Missing aurora token after fresh R13 login.")
        headers["aurora-token"] = token
        token_refreshed = True
        account_context = _request_account_context(session, headers)
        if _is_invalid_token_response(account_context):
            raise RuntimeError("R13 token is still invalid after one fresh login")
    disp_site_codes = _read_account_site_filter(account_context)

    while True:
        payload = _build_payload(
            start=start_value,
            end=end_value,
            disp_site_codes=disp_site_codes,
            page_size=page_size,
            page=current_page,
        )

        response = session.post(API_URL, json=payload, headers=headers, timeout=20)
        response.raise_for_status()
        data = response.json()
        if _is_invalid_token_response(data):
            if token_refreshed:
                raise RuntimeError("R13 token is still invalid after one fresh login")
            session = auth.login_and_get_session(
                username=username,
                password=password,
                account_key=account_key,
                exchange=False,
                verify=False,
                allow_cached=False,
            )
            token = auth.last_token
            if not token:
                raise RuntimeError("Missing aurora token after fresh R13 login.")
            headers["aurora-token"] = token
            refreshed_account_context = _request_account_context(session, headers)
            if _is_invalid_token_response(refreshed_account_context):
                raise RuntimeError("R13 token is still invalid after one fresh login")
            refreshed_site_codes = _read_account_site_filter(
                refreshed_account_context
            )
            if refreshed_site_codes != disp_site_codes:
                raise RuntimeError(
                    "R13 account site changed while fetching; refusing mixed results"
                )
            response = session.post(API_URL, json=payload, headers=headers, timeout=20)
            response.raise_for_status()
            data = response.json()
            if _is_invalid_token_response(data):
                raise RuntimeError("R13 token is still invalid after one fresh login")
            token_refreshed = True
        _require_success_response(data, label=f"R13 page {current_page}")
        if not _has_rows_container(data):
            raise RuntimeError(f"R13 page {current_page} response is missing a records list")
        rows = _extract_rows(data)
        page_total = _extract_total(data)
        if page_total is None:
            raise RuntimeError(
                f"R13 page {current_page} response is missing an authoritative total"
            )
        if expected_total is None:
            expected_total = page_total
        elif page_total != expected_total:
            raise RuntimeError("R13 pagination total changed while fetching")
        fetched_source_rows += len(rows)
        if expected_total is not None and fetched_source_rows > expected_total:
            raise RuntimeError("R13 fetched row count exceeds the declared total")

        for row in rows:
            if not isinstance(row, dict):
                raise RuntimeError(f"R13 page {current_page} contains a non-object row")
            legacy_bill_code = str(row.get("billNumberMain") or "").strip()
            current_bill_code = str(row.get("waybillNo") or "").strip()
            if legacy_bill_code and current_bill_code and legacy_bill_code != current_bill_code:
                raise RuntimeError(
                    f"R13 page {current_page} contains conflicting waybill identities"
                )
            bill_code = current_bill_code or legacy_bill_code
            if not bill_code:
                raise RuntimeError(
                    f"R13 page {current_page} contains a row without a waybill identity"
                )
            plan_sign_time = str(row.get("planSignTime") or "").strip()
            if not plan_sign_time:
                raise RuntimeError(
                    f"R13 page {current_page} contains a row without planSignTime"
                )
            normalized = {
                "billNumberMain": bill_code,
                "planSignTime": plan_sign_time,
                "goodsName": row.get("goodsName"),
                "pcs": _optional_nonnegative_int(row.get("pcs"), field="pcs"),
                "dispAddress": row.get("dispAddress"),
                "dispatchMode": row.get("dispatchMode"),
                "packTypeDesc": row.get("packTypeDesc"),
                "isSigns": row.get("isSigns"),
                "signSiteName": row.get("signSiteName"),
                "signTime": row.get("signTime"),
                "dispTime": row.get("dispTime"),
            }
            previous = seen_rows.get(bill_code)
            if previous is not None:
                if previous != normalized:
                    raise RuntimeError(f"R13 duplicate bill has conflicting data: {bill_code}")
                continue
            seen_rows[bill_code] = normalized
            result.append(normalized)

        fetched_pages += 1
        if not fetch_all:
            break
        if fetched_source_rows == expected_total:
            break
        if not rows or len(rows) < page_size:
            raise RuntimeError(
                "R13 pagination ended before the declared total was collected"
            )
        total_pages = max(1, ceil(expected_total / page_size))
        if current_page >= total_pages:
            raise RuntimeError(
                "R13 declared total does not match the fetched source row count"
            )
        if fetched_pages >= max_pages:
            raise RuntimeError(
                f"R13 pagination reached max_pages={max_pages} before a complete terminal page"
            )
        current_page += 1

    return result


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
    caller_site_keys = [
        key
        for key in ("disp_site_code", "dispSiteCode", "r13_site_code")
        if _has_value(params.get(key))
    ]
    if caller_site_keys:
        raise RuntimeError(
            "R13 site identity must come from the selected account session: "
            + ", ".join(sorted(caller_site_keys))
        )

    start = params.get("start")
    end = params.get("end")
    days = _coerce_int(params.get("days"), default=7)
    page_size = _coerce_int(params.get("page_size") or params.get("pageSize"), default=100)
    page = _coerce_int(params.get("page") or params.get("currentPage"), default=1)
    fetch_all = _coerce_bool(params.get("fetch_all") or params.get("fetchAll"), default=True)
    max_pages = _coerce_int(params.get("max_pages") or params.get("maxPages"), default=500)

    account_id = str(params.get("r13_account_id") or "").strip()
    if not account_id:
        raise RuntimeError("R13 account identity is required")

    return fetch_qianshou(
        config_path=params.get("config_path"),
        username=params.get("username") or params.get("user"),
        password=params.get("password") or params.get("pass"),
        account_key=params.get("account_key") or params.get("accountKey"),
        start=start,
        end=end,
        days=days,
        page_size=page_size,
        page=page,
        fetch_all=fetch_all,
        max_pages=max_pages,
        account_id=account_id,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Fetch r13 arrival pre-report data.")
    parser.add_argument("--config-path", default=None)
    parser.add_argument("--username", default=None)
    parser.add_argument("--password", default=None)
    parser.add_argument("--account-key", default=None)
    parser.add_argument("--account-id", required=True)
    parser.add_argument("--start", default=None, help="YYYY-MM-DD HH:MM:SS")
    parser.add_argument("--end", default=None, help="YYYY-MM-DD HH:MM:SS")
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--page-size", type=int, default=100)
    parser.add_argument("--page", type=int, default=1)
    parser.add_argument("--fetch-all", action="store_true")
    parser.add_argument("--max-pages", type=int, default=500)
    args = parser.parse_args()

    result = fetch_qianshou(
        config_path=args.config_path,
        username=args.username,
        password=args.password,
        account_key=args.account_key,
        start=args.start,
        end=args.end,
        days=args.days,
        page_size=args.page_size,
        page=args.page,
        fetch_all=args.fetch_all,
        max_pages=args.max_pages,
        account_id=args.account_id,
    )
    print(json.dumps(result, ensure_ascii=True, indent=2))


if __name__ == "__main__":
    main()
