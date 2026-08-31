"""Pure parsing, validation, and formatting helpers for Feishu previews."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from agent.feishu_command_contract import is_scan_cancel_text, is_scan_confirm_text
from agent.orchestration.models import OrchestrationError
from agent.orchestration.scan_preview_binding import normalize_preview_run_id


SELF_PICKUP_MAX_SELECTED = 250
SELECTION_PREVIEW_PENDING_TTL = 900
SCAN_PREVIEW_PENDING_TTL = 900
FEISHU_SAFE_TEXT_BYTES = 3500

SCAN_PREVIEW_ERROR_MESSAGES = {
    "SCAN_PREVIEW_ID_INVALID": "扫描预览标识无效，请重新发送“扫描”。",
    "SCAN_PREVIEW_NOT_FOUND": "扫描预览不存在，请重新发送“扫描”。",
    "SCAN_PREVIEW_INCOMPLETE": "扫描预览尚未完整生成，请重新发送“扫描”。",
    "SCAN_PREVIEW_INVALID": "扫描预览证据无效，请重新发送“扫描”。",
    "SCAN_PREVIEW_EXPIRED": "扫描预览已超过十五分钟，请重新发送“扫描”。",
    "SCAN_PREVIEW_STALE": "扫描数据已变化，请重新发送“扫描”后再确认。",
    "PROJECT_INVOCATION_STALE": "扫描项目配置已变化，请重新发送“扫描”。",
    "SCAN_PREVIEW_ALREADY_CONSUMED": "该预览已提交过正式请求，请前往事项中心查看原任务。",
    "REQUEST_ID_REUSED": "本次飞书事件标识已被使用，请重新发送“确认扫描”。",
    "SCAN_PREVIEW_FORMAL_EXECUTION_DISABLED": "正式扫描尚未开放，本次没有写入第三方系统。",
    "SCAN_PREVIEW_CONTEXT_REQUIRED": "服务端扫描合同缺少预览上下文，正式执行已阻断。",
    "SCAN_PREVIEW_CONTEXT_INVALID": "服务端扫描合同与预览不一致，正式执行已阻断。",
}

def split_text_chunks(
    lines: list[str],
    *,
    max_bytes: int = FEISHU_SAFE_TEXT_BYTES,
) -> list[str]:
    chunks: list[str] = []
    current: list[str] = []
    current_bytes = 0
    for raw_line in lines:
        line = str(raw_line)
        line_bytes = len(line.encode("utf-8"))
        if line_bytes > max_bytes:
            raise ValueError("单行分批消息超过飞书安全长度")
        separator_bytes = 1 if current else 0
        if current and current_bytes + separator_bytes + line_bytes > max_bytes:
            chunks.append("\n".join(current))
            current = []
            current_bytes = 0
            separator_bytes = 0
        current.append(line)
        current_bytes += separator_bytes + line_bytes
    if current:
        chunks.append("\n".join(current))
    return chunks


def parse_split_selection(text: str, candidate_count: int) -> list[int]:
    normalized = str(text or "").strip()
    if candidate_count <= 0:
        raise ValueError("当前没有可选择的运单")
    if normalized == "全部":
        return list(range(1, candidate_count + 1))
    for separator in ("，", "、"):
        normalized = normalized.replace(separator, ",")
    if (
        not normalized
        or normalized.startswith(",")
        or normalized.endswith(",")
        or ",," in normalized
    ):
        raise ValueError("请输入数字、逗号分隔数字、区间或“全部”")
    selected: list[int] = []
    seen: set[int] = set()
    for raw_token in normalized.split(","):
        token = raw_token.strip()
        if re.fullmatch(r"\d+", token):
            values = [int(token)]
        else:
            match = re.fullmatch(r"(\d+)\s*-\s*(\d+)", token)
            if not match:
                raise ValueError(f"非法序号：{token or '空'}")
            start = int(match.group(1))
            end = int(match.group(2))
            if start > end:
                raise ValueError(f"区间起始序号不能大于结束序号：{token}")
            values = list(range(start, end + 1))
        for value in values:
            if value < 1 or value > candidate_count:
                raise ValueError(f"序号越界：{value}（可选 1-{candidate_count}）")
            if value in seen:
                raise ValueError(f"序号重复或区间重叠：{value}")
            seen.add(value)
            selected.append(value)
    if not selected:
        raise ValueError("至少选择一个序号")
    return selected


def split_candidate_lines(
    candidates: list[dict[str, Any]],
    hidden_completed: int,
) -> list[str]:
    lines = [
        f"待执行分批运单 {len(candidates)} 单（已隐藏完整成功 {hidden_completed} 单）：",
    ]
    for index, item in enumerate(candidates, start=1):
        lines.append(
            f"{index}. {item.get('bill_code')} "
            f"[{item.get('problem_item_status') or item.get('status') or '未执行'}] "
            f"{item.get('problem_type')}，已到{item.get('arrived_quantity')}/"
            f"应到{item.get('expected_quantity')}件"
        )
    lines.extend(
        [
            "",
            "回复“确认”直接执行全部；如需部分上传，请输入序号：2 / 1,3,5 / 2-4。",
            "回复“取消”放弃；部分选择后需再次回复“确认”执行；15 分钟内有效。",
        ]
    )
    return lines


def split_selected_lines(selected: list[dict[str, Any]]) -> list[str]:
    lines = [f"已选择 {len(selected)} 单："]
    for item in selected:
        lines.append(
            f"- {item.get('bill_code')} "
            f"[{item.get('problem_item_status') or item.get('status') or '未执行'}] "
            f"{item.get('problem_type')}"
        )
    lines.extend(["", "回复“确认”正式执行，回复“取消”放弃。15 分钟内有效。"])
    return lines


def selection_preview_ttl(projection: dict[str, Any]) -> int:
    expires_at = datetime.fromisoformat(
        str(projection["expires_at"]).replace("Z", "+00:00")
    )
    remaining = int((expires_at - datetime.now(timezone.utc)).total_seconds())
    return max(1, min(SELECTION_PREVIEW_PENDING_TTL, remaining))


def selection_confirmation_ttl(pending: dict[str, Any]) -> int:
    try:
        expires_at = datetime.fromisoformat(
            str(pending.get("expires_at") or "").replace("Z", "+00:00")
        )
    except ValueError:
        return 0
    if expires_at.tzinfo is None:
        return 0
    remaining = int((expires_at - datetime.now(timezone.utc)).total_seconds())
    return max(0, min(SELECTION_PREVIEW_PENDING_TTL, remaining))


def normalize_selection_preview_projection(
    value: Any,
    *,
    expected_automation_id: str,
    expected_run_id: str,
) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    try:
        preview_run_id = normalize_preview_run_id(value.get("preview_run_id"))
        run_id = normalize_preview_run_id(expected_run_id)
        expires_at = datetime.fromisoformat(
            str(value.get("expires_at") or "").replace("Z", "+00:00")
        )
        observed_at = datetime.fromisoformat(
            str(value.get("observed_at") or "").replace("Z", "+00:00")
        )
    except (OrchestrationError, ValueError):
        return None
    raw_count = value.get("candidate_count")
    raw_candidates = value.get("candidates")
    if (
        value.get("contract_version") != 1
        or str(value.get("automation_id") or "") != expected_automation_id
        or preview_run_id != run_id
        or expires_at.tzinfo is None
        or observed_at.tzinfo is None
        or expires_at <= observed_at
        or type(raw_count) is not int
        or not isinstance(raw_candidates, list)
        or raw_count != len(raw_candidates)
        or not isinstance(value.get("summary"), dict)
        or type(value.get("can_confirm")) is not bool
    ):
        return None
    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw_candidate in raw_candidates:
        if not isinstance(raw_candidate, dict):
            return None
        bill_code = str(raw_candidate.get("bill_code") or "").strip()
        if not bill_code or len(bill_code) > 128 or bill_code in seen:
            return None
        seen.add(bill_code)
        candidate = dict(raw_candidate)
        candidate["bill_code"] = bill_code
        candidates.append(candidate)
    projection = dict(value)
    projection["preview_run_id"] = preview_run_id
    projection["candidates"] = candidates
    return projection


def self_pickup_candidate_lines(
    candidates: list[dict[str, Any]],
    duplicate_source_rows: int,
) -> list[str]:
    lines = [f"待上传自提到货问题件候选 {len(candidates)} 单："]
    for item in candidates:
        lines.append(f"- {item.get('bill_code')}")
    if duplicate_source_rows:
        lines.append(f"来源表重复行：{duplicate_source_rows} 行（已合并）")
    lines.append("")
    if not candidates:
        lines.append("当前没有需要上传的候选数据。")
    elif len(candidates) > SELF_PICKUP_MAX_SELECTED:
        lines.append(
            f"候选超过单次上限 {SELF_PICKUP_MAX_SELECTED} 单，本次未开放确认入口。"
        )
    else:
        lines.append("回复“确认”上传全部候选，回复“取消”放弃；15 分钟内有效。")
    return lines


def contains_account_override(value: Any) -> bool:
    if isinstance(value, dict):
        return any(
            _account_field_name(key) or contains_account_override(item)
            for key, item in value.items()
        )
    if isinstance(value, (list, tuple)):
        return any(contains_account_override(item) for item in value)
    return False


def _account_field_name(value: Any) -> bool:
    field_name = str(value or "").strip().lower()
    return (
        field_name in {"account_id", "account_ids"}
        or field_name.endswith(("_account_id", "_account_ids"))
    )


def scan_preview_error_message(error_code: Any) -> str:
    code = str(error_code or "").strip()
    return SCAN_PREVIEW_ERROR_MESSAGES.get(
        code,
        "扫描请求结果暂时无法确定，请前往事项中心查看原任务。",
    )


def scan_preview_ttl(projection: dict[str, Any]) -> int:
    expires_at = datetime.fromisoformat(
        str(projection["expires_at"]).replace("Z", "+00:00")
    )
    remaining = int((expires_at - datetime.now(timezone.utc)).total_seconds())
    return max(1, min(SCAN_PREVIEW_PENDING_TTL, remaining))


def scan_confirmation_ttl(pending: dict[str, Any]) -> int:
    try:
        expires_at = datetime.fromisoformat(
            str(pending.get("expires_at") or "").replace("Z", "+00:00")
        )
    except ValueError:
        return 0
    if expires_at.tzinfo is None:
        return 0
    remaining = int((expires_at - datetime.now(timezone.utc)).total_seconds())
    return max(0, min(SCAN_PREVIEW_PENDING_TTL, remaining))


def scan_preview_reply(projection: dict[str, Any]) -> str:
    return "\n".join(
        [
            "扫描预览已生成：",
            f"日期：{projection['target_date']}",
            f"来源页数：{projection['source_page_count']}",
            f"来源记录：{projection['normalized_record_count']}",
            f"待扫描：{projection['selection_count']}",
            f"提交批次：{projection['batch_count']}",
            f"有效期至：{projection['expires_at']}",
            "",
            "请在十五分钟内回复“确认扫描”执行正式扫描，或回复“取消扫描”放弃。",
        ]
    )
