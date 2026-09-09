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
    count_result = data.get("count_result") if isinstance(data, Mapping) else None
    if isinstance(count_result, Mapping):
        for field, label in (("child_scan_rows", "累计子单扫描条数"), ("quantity_gaps", "件数未齐单数")):
            value = count_result.get(field)
            if type(value) is int and value >= 0:
                parts.append(f"{label}：{value}")
    return "；".join(parts)
