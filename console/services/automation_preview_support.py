"""Private validation helpers for the automation preview projections.

The HTTP service owns request handling and task lifecycle orchestration.  This
module keeps the small, deterministic preview contracts and grouping helper
separate so that the service class remains below the repository's module-size
limit without changing its public import surface.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from console.app_support import normalize_feedback_text
from console.services.automation_projects import AUTOMATION_PROJECT_ID_RE


SCAN_PREVIEW_PROJECT_ID = "scan_codes"
SELECTION_PREVIEW_PROJECT_IDS = frozenset(
    {"self_pickup_problem_upload", "split_pending_problem_upload"}
)
SELECTION_PREVIEW_PUBLIC_FIELDS = frozenset(
    {
        "contract_version",
        "automation_id",
        "title",
        "preview_run_id",
        "observed_at",
        "expires_at",
        "candidate_count",
        "candidates",
        "summary",
        "can_confirm",
    }
)
SELECTION_PREVIEW_CANDIDATE_FIELDS = {
    "self_pickup_problem_upload": frozenset(
        {
            "arrival_count",
            "bill_code",
            "delivery_method",
            "destination_site",
            "goods_count",
            "row_number",
            "source_id",
            "source_name",
        }
    ),
    "split_pending_problem_upload": frozenset(
        {
            "arrived_quantity",
            "bill_code",
            "complaint_status",
            "expected_quantity",
            "pending_quantity",
            "problem_item_status",
            "problem_type",
            "source_row_no",
        }
    ),
}
SELECTION_PREVIEW_ERROR_MESSAGES = {
    "SELECTION_PREVIEW_PROJECT_INVALID": "该自动化不支持后台候选选择。",
    "SELECTION_PREVIEW_NOT_FOUND": "候选清单不存在，请重新读取。",
    "SELECTION_PREVIEW_INCOMPLETE": "候选清单尚未生成完成，请稍后重试。",
    "SELECTION_PREVIEW_INVALID": "候选清单校验失败，请重新读取。",
    "SELECTION_PREVIEW_EXPIRED": "候选清单已超过十五分钟，请重新读取。",
    "SELECTION_PREVIEW_STALE": "项目配置已变化，请重新读取候选清单。",
    "SELECTION_CHANGED": "来源数据已变化，请重新读取后再选择。",
    "SELECTION_REQUIRED": "请至少选择一票运单。",
    "SELECTION_INVALID": "所选运单无效，请重新选择。",
    "REQUEST_ID_REUSED": "本次请求标识已被使用，请重新点击确认。",
    "ACCOUNT_LOGIN_REQUIRED": "候选读取失败：业务账号登录已失效，请重新登录后再试。",
    "BROKER_ACCOUNT_UNAVAILABLE": "候选读取失败：所选业务账号当前不可用。",
    "BROKER_RESOURCE_INVALID": "候选读取失败：已绑定的数据位置无效，请重新选择。",
    "BROKER_RESOURCE_UNAVAILABLE": "候选读取失败：飞书数据位置暂时不可用，请检查权限和工作表状态。",
    "BROKER_SOURCE_FAILED": "候选读取失败：业务数据源暂时不可达，请稍后重试。",
    "BROKER_SOURCE_INVALID": "候选读取失败：来源字段结构已变化，请检查每日到货工作表。",
    "PROJECT_ROUTE_NOT_FOUND": "候选读取失败：该插件的项目运行位置尚未就绪。",
    "RESOURCE_PERMISSION_DENIED": "候选读取失败：当前账号无权读取所选飞书数据位置。",
    "RESOURCE_UNAVAILABLE": "候选读取失败：所选飞书数据位置已停用、被删除或不可用。",
    "RUNTIME_GENERATION_UNSTABLE": "候选读取失败：插件运行环境正在同步，请稍后重试。",
    "SOURCE_SCHEMA_CHANGED": "候选读取失败：每日到货工作表字段结构已变化，请检查表头。",
    "SOURCE_SHEET_NOT_FOUND": "候选读取失败：未找到绑定的每日到货工作表，请重新选择。",
    "SOURCE_UNAVAILABLE": "候选读取失败：每日到货数据暂时不可达，请稍后重试。",
}
SCAN_PREVIEW_PUBLIC_FIELDS = frozenset(
    {
        "contract_version",
        "preview_run_id",
        "target_date",
        "observed_at",
        "expires_at",
        "source_page_count",
        "normalized_record_count",
        "selection_count",
        "batch_count",
        "can_confirm",
    }
)
SCAN_PREVIEW_ERROR_MESSAGES = {
    "SCAN_PREVIEW_ID_INVALID": "扫描预览标识无效，请重新生成预览。",
    "SCAN_PREVIEW_NOT_FOUND": "扫描预览不存在，请重新生成预览。",
    "SCAN_PREVIEW_INCOMPLETE": "扫描预览尚未完整生成，请重新生成预览。",
    "SCAN_PREVIEW_INVALID": "扫描预览证据无效，请重新生成预览。",
    "SCAN_PREVIEW_EXPIRED": "扫描预览已超过十五分钟，请重新生成预览。",
    "SCAN_PREVIEW_STALE": "扫描数据已变化，请重新生成预览后再确认。",
    "PROJECT_INVOCATION_STALE": "项目配置已变化，请重新生成预览后再确认。",
    "SCAN_PREVIEW_ALREADY_CONSUMED": "该预览已提交过正式请求，请查询原 Run，不要重复执行。",
    "REQUEST_ID_REUSED": "本次请求标识已被使用，请重新点击确认。",
    "SCAN_PREVIEW_FORMAL_EXECUTION_DISABLED": "正式扫描尚未开放，本次没有写入第三方系统。",
    "SCAN_PREVIEW_CONTEXT_REQUIRED": "服务端扫描合同缺少预览上下文，正式执行已阻断。",
    "SCAN_PREVIEW_CONTEXT_INVALID": "服务端扫描合同与预览不一致，正式执行已阻断。",
}


def normalize_scan_preview_projection(
    raw: Any,
    *,
    expected_run_id: str,
) -> dict[str, Any] | None:
    """Accept only the frozen public scan preview contract."""

    if not isinstance(raw, Mapping) or set(raw) != SCAN_PREVIEW_PUBLIC_FIELDS:
        return None
    preview_run_id = str(raw.get("preview_run_id") or "").strip()
    try:
        normalized_preview_run_id = str(uuid.UUID(preview_run_id))
    except (ValueError, AttributeError):
        return None
    if normalized_preview_run_id != preview_run_id or preview_run_id != expected_run_id:
        return None
    if raw.get("contract_version") != 1 or not isinstance(raw.get("can_confirm"), bool):
        return None
    target_date = str(raw.get("target_date") or "").strip()
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", target_date):
        return None
    try:
        datetime.strptime(target_date, "%Y-%m-%d")
    except ValueError:
        return None
    timestamps: dict[str, str] = {}
    for field in ("observed_at", "expires_at"):
        value = str(raw.get(field) or "").strip()
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        if not value or len(value) > 64 or parsed.tzinfo is None:
            return None
        timestamps[field] = value
    counts: dict[str, int] = {}
    for field in (
        "source_page_count",
        "normalized_record_count",
        "selection_count",
        "batch_count",
    ):
        value = raw.get(field)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return None
        counts[field] = value
    return {
        "contract_version": 1,
        "preview_run_id": preview_run_id,
        "target_date": target_date,
        **timestamps,
        **counts,
        "can_confirm": raw["can_confirm"],
    }


def scan_preview_error_message(error_code: Any, fallback: Any = "") -> str:
    code = str(error_code or "").strip()
    if code in SCAN_PREVIEW_ERROR_MESSAGES:
        return SCAN_PREVIEW_ERROR_MESSAGES[code]
    return normalize_feedback_text(fallback or "扫描预览当前不可用，请重新生成。")


def normalize_selection_preview_projection(
    raw: Any,
    *,
    expected_automation_id: str,
    expected_run_id: str,
) -> dict[str, Any] | None:
    """Accept only the simple, signed public selection contract."""

    if not isinstance(raw, Mapping) or set(raw) != SELECTION_PREVIEW_PUBLIC_FIELDS:
        return None
    automation_id = str(raw.get("automation_id") or "").strip()
    if (
        automation_id != expected_automation_id
        or automation_id not in SELECTION_PREVIEW_PROJECT_IDS
    ):
        return None
    preview_run_id = str(raw.get("preview_run_id") or "").strip()
    try:
        normalized_preview_run_id = str(uuid.UUID(preview_run_id))
    except (ValueError, AttributeError):
        return None
    if normalized_preview_run_id != preview_run_id or preview_run_id != expected_run_id:
        return None
    if raw.get("contract_version") != 1 or not isinstance(raw.get("can_confirm"), bool):
        return None
    title = str(raw.get("title") or "").strip()
    if not title or len(title) > 80:
        return None
    timestamps: dict[str, str] = {}
    for field in ("observed_at", "expires_at"):
        value = str(raw.get(field) or "").strip()
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        if not value or len(value) > 64 or parsed.tzinfo is None:
            return None
        timestamps[field] = value
    candidate_count = raw.get("candidate_count")
    candidates = raw.get("candidates")
    allowed_fields = SELECTION_PREVIEW_CANDIDATE_FIELDS[automation_id]
    if (
        isinstance(candidate_count, bool)
        or not isinstance(candidate_count, int)
        or not 0 <= candidate_count <= 10_000
        or not isinstance(candidates, list)
        or len(candidates) != candidate_count
    ):
        return None
    normalized_candidates: list[dict[str, Any]] = []
    seen: set[str] = set()
    for candidate in candidates:
        if not isinstance(candidate, Mapping) or set(candidate) != allowed_fields:
            return None
        bill_code = str(candidate.get("bill_code") or "").strip()
        if not bill_code or len(bill_code) > 64 or bill_code in seen:
            return None
        seen.add(bill_code)
        normalized_candidates.append(dict(candidate))
    summary = raw.get("summary")
    if not isinstance(summary, Mapping):
        return None
    return {
        "contract_version": 1,
        "automation_id": automation_id,
        "title": title,
        "preview_run_id": preview_run_id,
        **timestamps,
        "candidate_count": candidate_count,
        "candidates": normalized_candidates,
        "summary": dict(summary),
        "can_confirm": raw["can_confirm"],
    }


def selection_preview_error_message(error_code: Any, fallback: Any = "") -> str:
    code = str(error_code or "").strip()
    if code in SELECTION_PREVIEW_ERROR_MESSAGES:
        return SELECTION_PREVIEW_ERROR_MESSAGES[code]
    return normalize_feedback_text(fallback or "候选清单当前不可用，请重新读取。")


def group_scheduled_rows_by_automation_id(
    rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Group only by the persisted project identity; never infer it from task IDs."""

    groups: list[dict[str, Any]] = []
    linked_groups: dict[str, dict[str, Any]] = {}
    for index, row in enumerate(rows):
        automation_id = str(row.get("automation_id") or "").strip()
        if AUTOMATION_PROJECT_ID_RE.fullmatch(automation_id):
            group = linked_groups.get(automation_id)
            if group is None:
                group = {
                    "storage_key": automation_id,
                    "task_id": automation_id,
                    "missing_automation_id": False,
                    "rows": [],
                }
                linked_groups[automation_id] = group
                groups.append(group)
            group["rows"].append(row)
            continue

        task_id = str(row.get("id") or "").strip()
        if not task_id:
            task_id = f"unlinked_scheduled_task_{index + 1}"
        groups.append(
            {
                "storage_key": f"__unlinked_scheduled_task__:{index}",
                "task_id": task_id,
                "missing_automation_id": True,
                "rows": [row],
            }
        )
    return groups


__all__ = [
    "SCAN_PREVIEW_PROJECT_ID",
    "SELECTION_PREVIEW_PROJECT_IDS",
    "SELECTION_PREVIEW_PUBLIC_FIELDS",
    "SELECTION_PREVIEW_CANDIDATE_FIELDS",
    "SELECTION_PREVIEW_ERROR_MESSAGES",
    "SCAN_PREVIEW_PUBLIC_FIELDS",
    "SCAN_PREVIEW_ERROR_MESSAGES",
    "normalize_scan_preview_projection",
    "scan_preview_error_message",
    "normalize_selection_preview_projection",
    "selection_preview_error_message",
    "group_scheduled_rows_by_automation_id",
]
