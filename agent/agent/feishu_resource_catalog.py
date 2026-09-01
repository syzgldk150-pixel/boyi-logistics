"""Resolve managed Feishu resources to their current human-readable names.

Only the server-side resource store calls this module.  Document tokens and
table identifiers never cross the managed-resource projection boundary.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
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
logger = logging.getLogger("agent")


class FeishuResourceCatalogError(RuntimeError):
    """A classified catalog failure that is safe to project as a stable code."""

    def __init__(self, message: str, *, code: str, global_failure: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.global_failure = global_failure


@dataclass(frozen=True)
class FeishuResourceResult:
    name: str
    status: str
    purpose: str
    problem_code: str


@dataclass(frozen=True)
class FeishuResourceCatalogResult:
    resources: Mapping[str, FeishuResourceResult]
    global_problem: str = ""


def refresh_feishu_resource_catalog() -> None:
    """Clear only process-local catalog caches before an explicit refresh."""

    global _token_cache
    with _cache_lock:
        _name_cache.clear()
        _token_cache = None


def _credentials() -> tuple[str, str]:
    app_id = str(os.getenv("FEISHU_APP_ID") or "").strip()
    app_secret = str(os.getenv("FEISHU_APP_SECRET") or "").strip()
    if not app_id or not app_secret:
        raise FeishuResourceCatalogError(
            "飞书应用凭据未配置",
            code="FEISHU_AUTH_UNAVAILABLE",
            global_failure=True,
        )
    return app_id, app_secret


def _response_payload(response: requests.Response) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError as exc:
        raise FeishuResourceCatalogError(
            "飞书返回了无法识别的数据",
            code="RESOURCE_TEMPORARILY_UNAVAILABLE",
        ) from exc
    if not isinstance(payload, dict):
        raise FeishuResourceCatalogError(
            "飞书返回了无法识别的数据",
            code="RESOURCE_TEMPORARILY_UNAVAILABLE",
        )
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
            if response.status_code in {401, 403}:
                raise FeishuResourceCatalogError(
                    "飞书拒绝读取表格目录",
                    code="RESOURCE_PERMISSION_DENIED",
                )
            if response.status_code == 404:
                raise FeishuResourceCatalogError(
                    "飞书表格不存在",
                    code="RESOURCE_NOT_FOUND",
                )
            response.raise_for_status()
            payload = _response_payload(response)
            if payload.get("code") not in (0, None):
                code = str(payload.get("code") or "")
                if code in {"99991663", "99991668", "99991671"}:
                    problem = "RESOURCE_PERMISSION_DENIED"
                elif code in {"1254004", "1254040", "1254302"}:
                    problem = "RESOURCE_NOT_FOUND"
                else:
                    problem = "RESOURCE_TEMPORARILY_UNAVAILABLE"
                raise FeishuResourceCatalogError(
                    "飞书拒绝读取表格目录",
                    code=problem,
                )
            return payload
        except (requests.RequestException, FeishuResourceCatalogError) as exc:
            last_error = exc
            if attempt + 1 < _MAX_ATTEMPTS:
                time.sleep(0.25 * (2**attempt))
                continue
    if isinstance(last_error, FeishuResourceCatalogError):
        raise last_error
    raise FeishuResourceCatalogError(
        "飞书表格目录暂时无法读取",
        code="FEISHU_CONNECTION_UNAVAILABLE",
        global_failure=True,
    ) from last_error


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
        raise FeishuResourceCatalogError(
            "飞书鉴权结果缺少访问凭证",
            code="FEISHU_AUTH_UNAVAILABLE",
            global_failure=True,
        )
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
        raise FeishuResourceCatalogError(
            "飞书表格目录缺少必要数据",
            code="RESOURCE_TEMPORARILY_UNAVAILABLE",
        )
    return value


def _spreadsheet_names(token: str, spreadsheet_token: str) -> tuple[str, dict[str, str]]:
    info = _data(_get(token, f"/open-apis/sheets/v3/spreadsheets/{spreadsheet_token}"))
    spreadsheet = info.get("spreadsheet")
    if not isinstance(spreadsheet, Mapping):
        spreadsheet = info
    document_name = str(spreadsheet.get("title") or "").strip()
    if not document_name:
        raise FeishuResourceCatalogError(
            "飞书电子表格缺少文档名称",
            code="RESOURCE_TEMPORARILY_UNAVAILABLE",
        )

    sheets_data = _data(
        _get(
            token,
            f"/open-apis/sheets/v3/spreadsheets/{spreadsheet_token}/sheets/query",
        )
    )
    raw_sheets = sheets_data.get("sheets")
    if not isinstance(raw_sheets, list):
        raise FeishuResourceCatalogError(
            "飞书电子表格缺少工作表目录",
            code="RESOURCE_TEMPORARILY_UNAVAILABLE",
        )
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
        raise FeishuResourceCatalogError(
            "飞书多维表格缺少文档名称",
            code="RESOURCE_TEMPORARILY_UNAVAILABLE",
        )

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
            raise FeishuResourceCatalogError(
                "飞书多维表格缺少数据表目录",
                code="RESOURCE_TEMPORARILY_UNAVAILABLE",
            )
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
            raise FeishuResourceCatalogError(
                "飞书多维表格分页信息不完整",
                code="RESOURCE_TEMPORARILY_UNAVAILABLE",
            )
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


def _purpose(config: Mapping[str, Any]) -> str:
    return str(
        config.get("display_name")
        or config.get("name")
        or config.get("title")
        or "业务数据"
    ).strip()[:80]


def resolve_live_feishu_resource_catalog(
    resources: Sequence[tuple[str, Mapping[str, Any]]],
    *,
    now: float | None = None,
) -> FeishuResourceCatalogResult:
    """Resolve each resource independently, preserving valid live names."""

    current = time.monotonic() if now is None else float(now)
    requested: dict[str, tuple[str, str, str]] = {}
    purposes: dict[str, str] = {}
    results: dict[str, FeishuResourceResult] = {}
    for resource_id, config in resources:
        kind = str(config.get("resource_kind") or "").strip().lower()
        if kind not in {"feishu_sheet", "feishu_bitable"}:
            continue
        safe_resource_id = str(resource_id)
        purposes[safe_resource_id] = _purpose(config)
        locator = _locator(kind, config)
        if not locator[1] or not locator[2]:
            results[safe_resource_id] = FeishuResourceResult(
                name="",
                status="unavailable",
                purpose=purposes[safe_resource_id],
                problem_code="RESOURCE_LOCATOR_MISSING",
            )
            logger.warning(
                "Feishu resource unavailable code=%s resource=%s",
                "RESOURCE_LOCATOR_MISSING",
                safe_resource_id,
            )
            continue
        requested[safe_resource_id] = locator
    if not requested:
        return FeishuResourceCatalogResult(resources=results)

    missing: dict[str, tuple[str, str, str]] = {}
    with _cache_lock:
        for resource_id, locator in requested.items():
            cached = _name_cache.get(locator)
            if cached is not None and cached[0] > current:
                results[resource_id] = FeishuResourceResult(
                    name=cached[1],
                    status="available",
                    purpose=purposes[resource_id],
                    problem_code="",
                )
            else:
                missing[resource_id] = locator
    if not missing:
        return _resolve_name_conflicts(results)

    try:
        access_token = _tenant_access_token(current)
    except FeishuResourceCatalogError as exc:
        code = exc.code if exc.global_failure else "FEISHU_AUTH_UNAVAILABLE"
        for resource_id in missing:
            results[resource_id] = FeishuResourceResult(
                name="",
                status="unavailable",
                purpose=purposes[resource_id],
                problem_code=code,
            )
        logger.warning("Feishu catalog unavailable code=%s", code)
        return FeishuResourceCatalogResult(resources=results, global_problem=code)

    sheet_catalogs: dict[str, tuple[str, dict[str, str]]] = {}
    bitable_catalogs: dict[str, tuple[str, dict[str, str]]] = {}
    refreshed: dict[tuple[str, str, str], str] = {}
    global_problem = ""
    for resource_id, locator in missing.items():
        kind, document_token, child_id = locator
        try:
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
        except FeishuResourceCatalogError as exc:
            if exc.global_failure:
                global_problem = exc.code
            results[resource_id] = FeishuResourceResult(
                name="",
                status="unavailable",
                purpose=purposes[resource_id],
                problem_code=exc.code,
            )
            logger.warning(
                "Feishu resource unavailable code=%s resource=%s",
                exc.code,
                resource_id,
            )
            continue
        document_name, children = catalog
        child_name = children.get(child_id)
        if not child_name:
            results[resource_id] = FeishuResourceResult(
                name="",
                status="unavailable",
                purpose=purposes[resource_id],
                problem_code="RESOURCE_NOT_FOUND",
            )
            logger.warning(
                "Feishu resource unavailable code=%s resource=%s",
                "RESOURCE_NOT_FOUND",
                resource_id,
            )
            continue
        display_name = f"{document_name} / {child_name}"
        refreshed[locator] = display_name
        results[resource_id] = FeishuResourceResult(
            name=display_name,
            status="available",
            purpose=purposes[resource_id],
            problem_code="",
        )
    with _cache_lock:
        expires_at = current + _CACHE_TTL_SECONDS
        for locator, display_name in refreshed.items():
            _name_cache[locator] = (expires_at, display_name)
    return _resolve_name_conflicts(results, global_problem=global_problem)


def _resolve_name_conflicts(
    resources: Mapping[str, FeishuResourceResult],
    *,
    global_problem: str = "",
) -> FeishuResourceCatalogResult:
    grouped: dict[str, list[str]] = {}
    for resource_id, result in resources.items():
        if result.status == "available":
            grouped.setdefault(result.name, []).append(resource_id)
    resolved = dict(resources)
    for name, resource_ids in grouped.items():
        if len(resource_ids) < 2:
            continue
        candidate_names = {
            resource_id: f"{name}（{resources[resource_id].purpose}）"
            for resource_id in resource_ids
        }
        if len(set(candidate_names.values())) == len(resource_ids):
            for resource_id, candidate in candidate_names.items():
                original = resources[resource_id]
                resolved[resource_id] = FeishuResourceResult(
                    name=candidate,
                    status="available",
                    purpose=original.purpose,
                    problem_code="",
                )
            continue
        for resource_id in resource_ids:
            original = resources[resource_id]
            resolved[resource_id] = FeishuResourceResult(
                name="",
                status="unavailable",
                purpose=original.purpose,
                problem_code="RESOURCE_NAME_CONFLICT",
            )
            logger.warning(
                "Feishu resource unavailable code=%s resource=%s",
                "RESOURCE_NAME_CONFLICT",
                resource_id,
            )
    return FeishuResourceCatalogResult(
        resources=resolved,
        global_problem=global_problem,
    )


def resolve_live_feishu_resource_names(
    resources: Sequence[tuple[str, Mapping[str, Any]]],
    *,
    now: float | None = None,
) -> dict[str, str]:
    """Compatibility view containing only currently usable live names."""

    catalog = resolve_live_feishu_resource_catalog(resources, now=now)
    return {
        resource_id: result.name
        for resource_id, result in catalog.resources.items()
        if result.status == "available"
    }


def _reset_caches_for_tests() -> None:
    refresh_feishu_resource_catalog()


__all__ = [
    "FeishuResourceCatalogError",
    "FeishuResourceCatalogResult",
    "FeishuResourceResult",
    "resolve_live_feishu_resource_catalog",
    "resolve_live_feishu_resource_names",
]
