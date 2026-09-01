"""Safe Console projection for managed automation resources."""

from __future__ import annotations

import re
from typing import Any, Mapping

from console.app_support import normalize_feedback_text
from shared.redaction import redact_text


_BINDING_ID_RE = re.compile(r"^[A-Za-z0-9_.:@/-]{1,160}$")
_KIND_RE = re.compile(r"[a-z][a-z0-9_.-]{0,63}")

RESOURCE_KIND_LABELS = {
    "feishu_sheet": "飞书电子表格",
    "feishu_bitable": "飞书多维表格",
    "feishu_route": "飞书消息入口",
    "webhook_route": "外部调用入口",
}

RESOURCE_PROBLEM_LABELS = {
    "FEISHU_AUTH_UNAVAILABLE": "飞书连接已失效，请检查飞书授权",
    "FEISHU_CONNECTION_UNAVAILABLE": "暂时无法连接飞书，请稍后刷新",
    "RESOURCE_CATALOG_UNAVAILABLE": "表格目录暂时无法读取，请稍后刷新",
    "RESOURCE_LOCATOR_MISSING": "未找到这项业务数据的飞书位置",
    "RESOURCE_PERMISSION_DENIED": "当前飞书应用没有读取这张表的权限",
    "RESOURCE_NOT_FOUND": "这张飞书表可能已被删除或移动",
    "RESOURCE_NAME_CONFLICT": "存在同名表格，暂时无法唯一识别",
    "RESOURCE_TEMPORARILY_UNAVAILABLE": "这张飞书表暂时无法读取",
}


def resource_display_name(
    resource_id: str,
    name: str,
    kind: str,
    known_names: Mapping[str, str],
) -> str:
    if name and name != resource_id:
        return name
    if kind in {"feishu_sheet", "feishu_bitable"}:
        return ""
    return known_names.get(resource_id) or "业务资源"


def normalize_plugin_resources(
    value: Any,
    *,
    known_names: Mapping[str, str],
) -> tuple[list[dict[str, str]], bool]:
    """Accept only the closed, credential-free resource projection."""

    if not isinstance(value, list):
        return [], False
    resources: list[dict[str, str]] = []
    seen: set[str] = set()
    fields = frozenset({"resource_id", "name", "kind", "status", "purpose", "problem_code"})
    legacy_fields = frozenset({"resource_id", "name", "kind", "status"})
    for raw in value:
        if not isinstance(raw, dict) or frozenset(raw) not in {fields, legacy_fields}:
            return [], False
        resource_id = str(raw.get("resource_id") or "").strip()
        name = normalize_feedback_text(redact_text(str(raw.get("name") or "")))[:160]
        kind = str(raw.get("kind") or "").strip().lower()
        status = str(raw.get("status") or "").strip().lower()
        purpose = normalize_feedback_text(redact_text(str(raw.get("purpose") or name)))[:80]
        problem_code = str(raw.get("problem_code") or "").strip().upper()
        invalid = (
            resource_id in seen
            or not _BINDING_ID_RE.fullmatch(resource_id)
            or not _KIND_RE.fullmatch(kind)
            or status not in {"available", "unavailable"}
            or (status == "available" and (not name or bool(problem_code)))
            or (status == "unavailable" and (bool(name) or not problem_code))
            or not purpose
            or (bool(problem_code) and problem_code not in RESOURCE_PROBLEM_LABELS)
        )
        if invalid:
            return [], False
        seen.add(resource_id)
        display_name = (
            resource_display_name(resource_id, name, kind, known_names)
            if status == "available"
            else purpose
        )
        if not display_name:
            return [], False
        resources.append(
            {
                "resource_id": resource_id,
                "name": name,
                "display_name": display_name,
                "kind": kind,
                "kind_label": RESOURCE_KIND_LABELS.get(kind, "业务资源"),
                "status": status,
                "purpose": purpose,
                "problem_code": problem_code,
                "problem_label": RESOURCE_PROBLEM_LABELS.get(problem_code, ""),
            }
        )
    return sorted(resources, key=lambda item: item["resource_id"]), True


__all__ = [
    "RESOURCE_KIND_LABELS",
    "RESOURCE_PROBLEM_LABELS",
    "normalize_plugin_resources",
    "resource_display_name",
]
