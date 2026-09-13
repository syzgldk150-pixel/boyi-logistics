"""Concise summaries built only from the current invocation's returned data."""

from collections.abc import Mapping


def invocation_count_summary(invocation: Mapping) -> str:
    result = invocation.get("result")
    if not isinstance(result, Mapping):
        return ""
    meta = result.get("meta")
    count = meta.get("record_count") if isinstance(meta, Mapping) else None
    parts = [f"处理记录：{count} 条"] if type(count) is int and count >= 0 else []
    data = result.get("data")
    if isinstance(data, Mapping) and data.get("projection_policy") == "existing_only":
        preview = data.get("dry_run") is True
        for field, label in (("updated", "飞书待更新" if preview else "飞书更新"),
                             ("database_would_update" if preview else "database_updated", "后台待更新" if preview else "后台更新"),
                             ("not_in_database_count", "未入库")):
            value = data.get(field)
            if type(value) is int and value >= 0:
                parts.append(f"{label}：{value} 条")
        codes = data.get("not_in_database_bill_codes")
        if isinstance(codes, list) and codes and all(isinstance(code, str) for code in codes):
            parts.append("未入库单号：" + "、".join(codes))
    count_result = data.get("count_result") if isinstance(data, Mapping) else None
    if isinstance(count_result, Mapping):
        for field, label in (("child_scan_rows", "累计子单扫描条数"), ("quantity_gaps", "件数未齐单数")):
            value = count_result.get(field)
            if type(value) is int and value >= 0:
                parts.append(f"{label}：{value}")
    return "；".join(parts)
