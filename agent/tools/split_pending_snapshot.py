"""Shared snapshot logic for split and not-arrived waybills."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any, Callable

from tools.feishu_cli_tool import feishu_operation
from tools.phase7_mysql_store import WAYBILL_EXPORT_HEADERS, replace_split_pending_problem_items
from tools.phase7_sync_common import get_required_resource


TARGET_RESOURCE_KEY = "phase7.split_pending_target_sheet"
EXPECTED_SPREADSHEET_TOKEN = "F0NVsI5dlhaWugtw14YcmdrQnvh"
EXPECTED_TARGET_SHEET_ID = "bNhh7u"

TARGET_HEADERS = ["运单编号", *WAYBILL_EXPORT_HEADERS]


def clean_text(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    if text.startswith("="):
        text = text[1:].strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {"'", '"'}:
        text = text[1:-1].strip()
    if text.endswith(".0") and text[:-2].isdigit():
        text = text[:-2]
    return text


def _parse_integer(value: Any, *, row_number: int, label: str) -> int:
    text = clean_text(value).replace(",", "")
    if not text:
        raise ValueError(f"第 {row_number} 行{label}为空")
    try:
        number = Decimal(text)
    except InvalidOperation as exc:
        raise ValueError(f"第 {row_number} 行{label}不是整数: {text}") from exc
    if not number.is_finite() or number != number.to_integral_value():
        raise ValueError(f"第 {row_number} 行{label}不是整数: {text}")
    return int(number)


def _validate_headers(headers: list[Any]) -> None:
    padded = list(headers[:19]) + [""] * max(0, 19 - len(headers))
    normalized = [clean_text(value) for value in padded]
    if "运单编号" not in normalized[0] and normalized[0] != "单号":
        raise ValueError("每日到货表 A 列表头不是运单编号")
    for index, expected in enumerate(TARGET_HEADERS[1:18], start=1):
        if normalized[index] != expected:
            raise ValueError(f"每日到货表 {chr(ord('A') + index)} 列表头应为“{expected}”")
    if normalized[18] not in {"累计到货件数", "已到货件数", "到货件数"}:
        raise ValueError("每日到货表 S 列不是累计到货件数")


def classify_sheet_values(values: list[list[Any]]) -> tuple[list[dict[str, Any]], int]:
    """Validate a 19-column arrival snapshot and return only incomplete waybills."""
    if not values:
        raise ValueError("每日到货表读取结果为空，保留原快照")
    _validate_headers(values[0])
    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()
    source_rows = 0
    for row_number, raw_row in enumerate(values[1:], start=2):
        row = list(raw_row[:19]) + [""] * max(0, 19 - len(raw_row))
        if not any(clean_text(value) for value in row):
            continue
        bill_code = clean_text(row[0])
        if not bill_code:
            raise ValueError(f"第 {row_number} 行存在数据但缺少运单编号")
        source_rows += 1
        if bill_code in seen:
            raise ValueError(f"每日到货表存在重复运单号 {bill_code}（第 {row_number} 行）")
        seen.add(bill_code)
        expected = _parse_integer(row[4], row_number=row_number, label="应到件数")
        arrived = _parse_integer(row[18], row_number=row_number, label="累计到货件数")
        if expected <= 0:
            raise ValueError(f"第 {row_number} 行应到件数必须大于 0")
        if arrived < 0:
            raise ValueError(f"第 {row_number} 行累计到货件数不能为负数")
        if arrived > expected:
            raise ValueError(f"第 {row_number} 行累计到货件数大于应到件数")
        if arrived == expected:
            continue
        if arrived == 0:
            problem_type = "有发未到"
            problem_owner_type = "通知类（不顺延时效）"
            problem_cause = "有发未到"
        else:
            problem_type = "少货/分批"
            problem_owner_type = "交接异常"
            problem_cause = f"应到{expected}件，已到{arrived}件"
        sheet_row = list(row[:18]) + [arrived]
        candidates.append(
            {
                "tracking_number": bill_code,
                "bill_code": bill_code,
                "source_row_no": row_number,
                "destination_station": clean_text(row[9]),
                "expected_quantity": expected,
                "arrived_quantity": arrived,
                "pending_quantity": expected - arrived,
                "problem_type": problem_type,
                "problem_owner_type": problem_owner_type,
                "problem_cause": problem_cause,
                "sheet_values": sheet_row,
            }
        )
    if source_rows == 0:
        raise ValueError("每日到货表没有有效运单数据，保留原快照")
    return candidates, source_rows


def _sheet_ref(resource: dict[str, Any]) -> tuple[str, str]:
    spreadsheet_token = clean_text(resource.get("spreadsheet_token"))
    value_range = clean_text(resource.get("range"))
    sheet_id = clean_text(resource.get("sheet_id"))
    if not sheet_id and "!" in value_range:
        sheet_id = value_range.split("!", 1)[0]
    if not spreadsheet_token or not value_range or not sheet_id:
        raise ValueError(f"{TARGET_RESOURCE_KEY} 缺少 spreadsheet_token、sheet_id 或 range")
    if spreadsheet_token != EXPECTED_SPREADSHEET_TOKEN:
        raise ValueError(f"{TARGET_RESOURCE_KEY} 未绑定指定的每日到货文档")
    if sheet_id != EXPECTED_TARGET_SHEET_ID:
        raise ValueError(f"{TARGET_RESOURCE_KEY} 绑定了错误的 sheet_id: {sheet_id}")
    return spreadsheet_token, sheet_id


def sync_target_sheet(candidates: list[dict[str, Any]]) -> dict[str, Any]:
    """Replace the target Sheet; zero candidates intentionally clears stale rows."""
    resource = get_required_resource(TARGET_RESOURCE_KEY)
    spreadsheet_token, sheet_id = _sheet_ref(resource)
    clear_range = clean_text(resource.get("clear_range") or f"{sheet_id}!A2:S5000")
    clear_result = feishu_operation(
        "clear_sheet",
        {
            "spreadsheet_token": spreadsheet_token,
            "sheet_id": sheet_id,
            "range": clear_range,
            "as": "bot",
        },
    )
    if not isinstance(clear_result, dict) or clear_result.get("error"):
        detail = str(clear_result.get("error") if isinstance(clear_result, dict) else clear_result)[:300]
        raise RuntimeError(f"清空分批及有发未到表失败: {detail}")
    values = [TARGET_HEADERS, *[item["sheet_values"] for item in candidates]]
    write_result = feishu_operation(
        "write_sheet",
        {
            "spreadsheet_token": spreadsheet_token,
            "sheet_id": sheet_id,
            "range": f"{sheet_id}!A1:S{len(values)}",
            "values": values,
            "as": "bot",
        },
    )
    if not isinstance(write_result, dict) or write_result.get("error"):
        detail = str(write_result.get("error") if isinstance(write_result, dict) else write_result)[:300]
        raise RuntimeError(f"写入分批及有发未到表失败: {detail}")
    return {
        "ok": True,
        "resource_key": TARGET_RESOURCE_KEY,
        "sheet_id": sheet_id,
        "rows": len(candidates),
        "clear_result": clear_result,
        "write_result": write_result,
    }


def refresh_snapshot(
    values: list[list[Any]],
    *,
    dry_run: bool = False,
    database_writer: Callable[[list[dict[str, Any]]], dict[str, Any]] = replace_split_pending_problem_items,
    sheet_writer: Callable[[list[dict[str, Any]]], dict[str, Any]] = sync_target_sheet,
) -> dict[str, Any]:
    """Refresh the internal state and target Sheet from one validated statistics result."""
    candidates, source_rows = classify_sheet_values(values)
    quantities = {
        "expected_min": min((item["expected_quantity"] for item in candidates), default=None),
        "expected_max": max((item["expected_quantity"] for item in candidates), default=None),
        "arrived_min": min((item["arrived_quantity"] for item in candidates), default=None),
        "arrived_max": max((item["arrived_quantity"] for item in candidates), default=None),
        "pending_min": min((item["pending_quantity"] for item in candidates), default=None),
        "pending_max": max((item["pending_quantity"] for item in candidates), default=None),
    }
    summary = {
        "ok": True,
        "source_rows": source_rows,
        "complete_rows": source_rows - len(candidates),
        "rows": len(candidates),
        "type_counts": {
            "少货/分批": sum(1 for item in candidates if item["problem_type"] == "少货/分批"),
            "有发未到": sum(1 for item in candidates if item["problem_type"] == "有发未到"),
        },
        "quantity_summary": quantities,
    }
    if dry_run:
        return {
            **summary,
            "skipped": True,
            "reason": "dry_run",
            "database_result": {"ok": True, "skipped": True, "rows": 0},
            "target_sheet_result": {"ok": True, "skipped": True, "rows": 0},
        }
    database_result = database_writer(candidates)
    target_sheet_result = sheet_writer(candidates)
    return {
        **summary,
        "database_result": database_result,
        "target_sheet_result": target_sheet_result,
    }
