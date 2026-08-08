"""Shared Yunda report endpoint helpers."""

from __future__ import annotations

import datetime as dt
from typing import Any
from urllib.parse import urljoin


REPORT_ORIGIN = "https://rpts-kyrpts.yunda56.com:8081"
REPORT_PAGE_PATH = "/kyrpts/page/kywdop/mrt_brch_frgt/mrt_s_brch_frgt_amt_tot"
SEARCH_PATH = "/kyrpts/mrt_s_brch_frgt_amt_tot/searchData"
DEFAULT_DEST_BRANCH = "56739382"
DEFAULT_PAGE_SIZE = 200
DEFAULT_MAX_PAGES = 100


def report_origin(params: dict[str, Any] | None = None) -> str:
    params = params or {}
    return str(params.get("report_origin") or REPORT_ORIGIN).rstrip("/")


def report_url(path: str, params: dict[str, Any] | None = None) -> str:
    return urljoin(report_origin(params) + "/", str(path or "").lstrip("/"))


def page_url(params: dict[str, Any] | None = None) -> str:
    return report_url(REPORT_PAGE_PATH, params)


def search_url(params: dict[str, Any] | None = None) -> str:
    return report_url(SEARCH_PATH, params)


def build_search_params(
    params: dict[str, Any] | None = None,
    *,
    target_date: dt.date,
    limit: int,
    offset: int,
) -> dict[str, Any]:
    params = params or {}
    return {
        "order": str(params.get("order") or "asc"),
        "limit": limit,
        "offset": offset,
        "bgn_dt": f"{target_date:%Y-%m-%d} 00:00:00",
        "end_dt": f"{target_date:%Y-%m-%d} 23:59:59",
        "dest_dbct": str(params.get("dest_dbct") or ""),
        "dest_brch": str(params.get("dest_brch") or params.get("dest_branch") or DEFAULT_DEST_BRANCH),
        "prod_typ": str(params.get("prod_typ") or ""),
        "if_same_city": str(params.get("if_same_city") or ""),
        "two_brch_check": str(params.get("two_brch_check") or "check"),
        "maxwgt": str(params.get("maxwgt") or ""),
        "minwgt": str(params.get("minwgt") or ""),
        "maxvol": str(params.get("maxvol") or ""),
        "minvol": str(params.get("minvol") or ""),
        "brch_id": str(params.get("brch_id") or ""),
        "order_typ": str(params.get("order_typ") or ""),
        "ship_typ": str(params.get("ship_typ") or ""),
    }
