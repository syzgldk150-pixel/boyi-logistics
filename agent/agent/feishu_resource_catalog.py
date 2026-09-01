"""Resolve managed Feishu resources to their current human-readable names.

Only the server-side resource store calls this module.  Document tokens and
table identifiers never cross the managed-resource projection boundary.
"""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Mapping, Sequence
from typing import Any
from urllib.parse import urlencode

import requests


_API_ROOT = "https://open.feishu.cn"
_CACHE_TTL_SECONDS = 300.0
_REQUEST_TIMEOUT_SECONDS = 12
_MAX_ATTEMPTS = 3

_cache_lock = threading.RLock()
_name_cache: dict[tuple[str, str, str], tuple[float, str]] = {}
_token_cache: tuple[float, str] | None = None


class FeishuResourceCatalogError(RuntimeError):
    """Raised when a complete, current Feishu resource catalog is unavailable."""


def _credentials() -> tuple[str, str]:
    app_id = str(os.getenv("FEISHU_APP_ID") or "").strip()
    app_secret = str(os.getenv("FEISHU_APP_SECRET") or "").strip()
    if not app_id or not app_secret:
        raise FeishuResourceCatalogError("飞书应用凭据未配置")
    return app_id, app_secret


def _response_payload(response: requests.Response) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError as exc:
        raise FeishuResourceCatalogError("飞书返回了无法识别的数据") from exc
    if not isinstance(payload, dict):
        raise FeishuResourceCatalogError("飞书返回了无法识别的数据")
    return payload


def _request_json(
    method: str,
    path: str,
    *,
    headers: Mapping[str, str] | None = None,
    json_body: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    last_error: Exception | None = None
    for attempt in range(_MAX_ATTEMPTS):
        try:
            response = requests.request(
                method,
                f"{_API_ROOT}{path}",
                headers=dict(headers or {}),
                json=dict(json_body) if json_body is not None else None,
                timeout=_REQUEST_TIMEOUT_SECONDS,
            )
            if response.status_code == 429 or response.status_code >= 500:
                if attempt + 1 < _MAX_ATTEMPTS:
                    time.sleep(0.25 * (2**attempt))
                    continue
            response.raise_for_status()
            payload = _response_payload(response)
            if payload.get("code") not in (0, None):
                raise FeishuResourceCatalogError("飞书拒绝读取表格目录")
            return payload
        except (requests.RequestException, FeishuResourceCatalogError) as exc:
            last_error = exc
            if attempt + 1 < _MAX_ATTEMPTS:
                time.sleep(0.25 * (2**attempt))
                continue
    raise FeishuResourceCatalogError("飞书表格目录暂时无法读取") from last_error


def _tenant_access_token(now: float) -> str:
    global _token_cache
    with _cache_lock:
        if _token_cache is not None and _token_cache[0] > now:
            return _token_cache[1]
    app_id, app_secret = _credentials()
    payload = _request_json(
        "POST",
        "/open-apis/auth/v3/tenant_access_token/internal",
        json_body={"app_id": app_id, "app_secret": app_secret},
    )
    token = str(payload.get("tenant_access_token") or "").strip()
    if not token:
        raise FeishuResourceCatalogError("飞书鉴权结果缺少访问凭证")
    try:
        expires_in = max(60, int(payload.get("expire") or 7200))
    except (TypeError, ValueError):
        expires_in = 7200
    with _cache_lock:
        _token_cache = (now + max(60, expires_in - 300), token)
    return token


def _get(token: str, path: str) -> dict[str, Any]:
    return _request_json("GET", path, headers={"Authorization": f"Bearer {token}"})


def _data(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    value = payload.get("data")
    if not isinstance(value, Mapping):
        raise FeishuResourceCatalogError("飞书表格目录缺少必要数据")
    return value


def _spreadsheet_names(token: str, spreadsheet_token: str) -> tuple[str, dict[str, str]]:
    info = _data(_get(token, f"/open-apis/sheets/v3/spreadsheets/{spreadsheet_token}"))
    spreadsheet = info.get("spreadsheet")
    if not isinstance(spreadsheet, Mapping):
        spreadsheet = info
    document_name = str(spreadsheet.get("title") or "").strip()
    if not document_name:
        raise FeishuResourceCatalogError("飞书电子表格缺少文档名称")

    sheets_data = _data(
        _get(
            token,
            f"/open-apis/sheets/v3/spreadsheets/{spreadsheet_token}/sheets/query",
        )
    )
    raw_sheets = sheets_data.get("sheets")
    if not isinstance(raw_sheets, list):
        raise FeishuResourceCatalogError("飞书电子表格缺少工作表目录")
    sheets: dict[str, str] = {}
    for raw in raw_sheets:
        if not isinstance(raw, Mapping):
            continue
        sheet_id = str(raw.get("sheet_id") or "").strip()
        title = str(raw.get("title") or "").strip()
        if sheet_id and title:
            sheets[sheet_id] = title
    return document_name, sheets


def _bitable_names(token: str, base_token: str) -> tuple[str, dict[str, str]]:
    info = _data(_get(token, f"/open-apis/bitable/v1/apps/{base_token}"))
    app = info.get("app")
    if not isinstance(app, Mapping):
        app = info
    document_name = str(app.get("name") or "").strip()
    if not document_name:
        raise FeishuResourceCatalogError("飞书多维表格缺少文档名称")

    tables: dict[str, str] = {}
    page_token = ""
    while True:
        query = {"page_size": 100}
        if page_token:
            query["page_token"] = page_token
        page = _data(
            _get(
                token,
                f"/open-apis/bitable/v1/apps/{base_token}/tables?{urlencode(query)}",
            )
        )
        raw_tables = page.get("items")
        if not isinstance(raw_tables, list):
            raw_tables = page.get("tables")
        if not isinstance(raw_tables, list):
            raise FeishuResourceCatalogError("飞书多维表格缺少数据表目录")
        for raw in raw_tables:
            if not isinstance(raw, Mapping):
                continue
            table_id = str(raw.get("table_id") or "").strip()
            name = str(raw.get("name") or "").strip()
            if table_id and name:
                tables[table_id] = name
        if page.get("has_more") is not True:
            break
        page_token = str(page.get("page_token") or "").strip()
        if not page_token:
            raise FeishuResourceCatalogError("飞书多维表格分页信息不完整")
    return document_name, tables


def _sheet_id(config: Mapping[str, Any]) -> str:
    sheet_id = str(config.get("sheet_id") or "").strip()
    if sheet_id:
        return sheet_id
    for field in ("range", "sheet_range", "default_write_range", "source_snapshot_range"):
        value = str(config.get(field) or "").strip()
        if "!" in value:
            return value.split("!", 1)[0].strip()
    return ""


def _locator(resource_kind: str, config: Mapping[str, Any]) -> tuple[str, str, str]:
    if resource_kind == "feishu_sheet":
        return (
            resource_kind,
            str(config.get("spreadsheet_token") or "").strip(),
            _sheet_id(config),
        )
    if resource_kind == "feishu_bitable":
        return (
            resource_kind,
            str(config.get("base_token") or "").strip(),
            str(config.get("table_id") or "").strip(),
        )
    return (resource_kind, "", "")


def resolve_live_feishu_resource_names(
    resources: Sequence[tuple[str, Mapping[str, Any]]],
    *,
    now: float | None = None,
) -> dict[str, str]:
    """Return current ``文档 / 工作表`` names for every Feishu resource.

    The result is all-or-nothing.  Missing credentials, permissions, metadata
    or one requested table fails the complete projection instead of exposing a
    stale or invented name.
    """

    current = time.monotonic() if now is None else float(now)
    requested: dict[str, tuple[str, str, str]] = {}
    for resource_id, config in resources:
        kind = str(config.get("resource_kind") or "").strip().lower()
        if kind not in {"feishu_sheet", "feishu_bitable"}:
            continue
        locator = _locator(kind, config)
        if not locator[1] or not locator[2]:
            raise FeishuResourceCatalogError(f"资源 {resource_id} 缺少飞书定位信息")
        requested[str(resource_id)] = locator
    if not requested:
        return {}

    resolved: dict[str, str] = {}
    missing: dict[str, tuple[str, str, str]] = {}
    with _cache_lock:
        for resource_id, locator in requested.items():
            cached = _name_cache.get(locator)
            if cached is not None and cached[0] > current:
                resolved[resource_id] = cached[1]
            else:
                missing[resource_id] = locator
    if not missing:
        return resolved

    access_token = _tenant_access_token(current)
    sheet_catalogs: dict[str, tuple[str, dict[str, str]]] = {}
    bitable_catalogs: dict[str, tuple[str, dict[str, str]]] = {}
    refreshed: dict[tuple[str, str, str], str] = {}
    for resource_id, locator in missing.items():
        kind, document_token, child_id = locator
        if kind == "feishu_sheet":
            catalog = sheet_catalogs.get(document_token)
            if catalog is None:
                catalog = _spreadsheet_names(access_token, document_token)
                sheet_catalogs[document_token] = catalog
        else:
            catalog = bitable_catalogs.get(document_token)
            if catalog is None:
                catalog = _bitable_names(access_token, document_token)
                bitable_catalogs[document_token] = catalog
        document_name, children = catalog
        child_name = children.get(child_id)
        if not child_name:
            raise FeishuResourceCatalogError(f"资源 {resource_id} 对应的飞书表格不存在")
        display_name = f"{document_name} / {child_name}"
        refreshed[locator] = display_name
        resolved[resource_id] = display_name
    with _cache_lock:
        expires_at = current + _CACHE_TTL_SECONDS
        for locator, display_name in refreshed.items():
            _name_cache[locator] = (expires_at, display_name)
    return resolved


def _reset_caches_for_tests() -> None:
    global _token_cache
    with _cache_lock:
        _name_cache.clear()
        _token_cache = None


__all__ = [
    "FeishuResourceCatalogError",
    "resolve_live_feishu_resource_names",
]
