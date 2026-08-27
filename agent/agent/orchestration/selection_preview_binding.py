"""Persisted preview binding for Console problem-item selections."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from agent.orchestration.models import OrchestrationError
from agent.orchestration.scan_preview_binding import normalize_preview_run_id
from shared.automation_project_authorization import canonical_sha256


SELECTION_PREVIEW_TTL = timedelta(minutes=15)
SELECTION_PREVIEW_PROJECTS: Mapping[str, Mapping[str, Any]] = {
    "self_pickup_problem_upload": {
        "plugin_id": "self_pickup_problem_upload",
        "title": "自提到货问题件",
        "candidate_fields": frozenset(
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
    },
    "split_pending_problem_upload": {
        "plugin_id": "split_pending_problem_upload",
        "title": "分批/未到问题件",
        "candidate_fields": frozenset(
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
    },
}
_HEX_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_MAX_CANDIDATES = 10_000


@dataclass(frozen=True)
class SelectionPreviewExpectation:
    project_instance_id: str
    plugin_id: str
    generation: int
    contract_digest: str
    configuration_version: int


@dataclass(frozen=True)
class _PersistedSelectionPreview:
    run_id: str
    fingerprint: str
    candidates: tuple[Mapping[str, Any], ...]
    observed_at: datetime
    expires_at: datetime
    data: Mapping[str, Any]


def is_selection_preview_project(entry: Any) -> bool:
    automation_id = str(getattr(entry, "automation_id", "") or "").strip()
    spec = SELECTION_PREVIEW_PROJECTS.get(automation_id)
    return bool(
        spec
        and str(getattr(entry, "plugin_id", "") or "").strip()
        == str(spec["plugin_id"])
        and str(getattr(entry, "trust_source", "") or "").strip()
        == "ed25519_first_party"
    )


def selection_preview_public_projection(
    uow: Any,
    *,
    preview_run_id: str,
    expectation: SelectionPreviewExpectation,
    now: datetime,
) -> dict[str, Any]:
    persisted = _load_preview(
        uow,
        preview_run_id=preview_run_id,
        expectation=expectation,
        now=now,
    )
    spec = SELECTION_PREVIEW_PROJECTS[expectation.project_instance_id]
    return {
        "contract_version": 1,
        "automation_id": expectation.project_instance_id,
        "title": str(spec["title"]),
        "preview_run_id": persisted.run_id,
        "observed_at": _iso_utc(persisted.observed_at),
        "expires_at": _iso_utc(persisted.expires_at),
        "candidate_count": len(persisted.candidates),
        "candidates": [dict(item) for item in persisted.candidates],
        "summary": _summary(expectation.project_instance_id, persisted.data),
        "can_confirm": _aware_utc(now, "now") < persisted.expires_at,
    }


def selection_confirmation_arguments(
    uow: Any,
    *,
    preview_run_id: str,
    expectation: SelectionPreviewExpectation,
    selected_bill_codes: Sequence[str],
    now: datetime,
) -> dict[str, Any]:
    persisted = _load_preview(
        uow,
        preview_run_id=preview_run_id,
        expectation=expectation,
        now=now,
    )
    checked_now = _aware_utc(now, "now")
    if checked_now >= persisted.expires_at:
        raise _error("SELECTION_PREVIEW_EXPIRED", "候选清单已超过十五分钟，请重新生成。")
    selected = _selected_bill_codes(selected_bill_codes)
    available = {str(item["bill_code"]) for item in persisted.candidates}
    unavailable = [item for item in selected if item not in available]
    if unavailable:
        raise _error("SELECTION_CHANGED", "所选运单已不在当前候选清单中，请重新生成。")
    return {
        "dry_run": False,
        "selected_bill_codes": selected,
        "preview_fingerprint": persisted.fingerprint,
    }


def _load_preview(
    uow: Any,
    *,
    preview_run_id: str,
    expectation: SelectionPreviewExpectation,
    now: datetime,
) -> _PersistedSelectionPreview:
    safe_run_id = normalize_preview_run_id(preview_run_id)
    spec = SELECTION_PREVIEW_PROJECTS.get(expectation.project_instance_id)
    if spec is None or expectation.plugin_id != spec["plugin_id"]:
        raise _error("SELECTION_PREVIEW_PROJECT_INVALID", "该项目不支持后台候选选择。")
    run = uow.runs.get(safe_run_id, for_update=False)
    if run is None:
        raise _error("SELECTION_PREVIEW_NOT_FOUND", "没有找到本次候选清单。")
    if str(run.get("status") or "") != "COMPLETED":
        raise _error("SELECTION_PREVIEW_INCOMPLETE", "候选清单尚未生成完成。")

    command_id = str(run.get("command_id") or "").strip()
    command = uow.commands.get(command_id, for_update=False) if command_id else None
    if command is None or str(command.get("command_type") or "") != "automation.project.invoke":
        raise _error("SELECTION_PREVIEW_INVALID", "候选清单没有可信运行记录。")
    invocation = _mapping_field(command, "automation_invocation_json", "automation_invocation")
    if (
        str(invocation.get("automation_id") or "") != expectation.project_instance_id
        or int(invocation.get("automation_generation") or 0) != expectation.generation
        or str(invocation.get("contract_hash") or "") != expectation.contract_digest
        or int(invocation.get("project_configuration_version") or 0)
        != expectation.configuration_version
    ):
        raise _error("SELECTION_PREVIEW_STALE", "项目配置已变化，请重新生成候选清单。")

    parameters = _mapping_field(command, "parameters_json", "parameters")
    expected_tool = f"automation.{expectation.project_instance_id}.run"
    arguments = parameters.get("arguments")
    if (
        str(parameters.get("tool_name") or "") != expected_tool
        or not isinstance(arguments, Mapping)
        or arguments.get("dry_run") is not True
        or arguments.get("selected_bill_codes") not in (None, [])
        or str(arguments.get("preview_fingerprint") or "")
    ):
        raise _error("SELECTION_PREVIEW_INVALID", "运行记录不是只读候选预览。")

    steps = uow.steps.list_for_run(safe_run_id)
    if len(steps) != 1:
        raise _error("SELECTION_PREVIEW_INVALID", "候选预览的运行步骤不完整。")
    step = steps[0]
    if (
        str(step.get("status") or "") != "COMPLETED"
        or str(step.get("postcondition_status") or "") != "VERIFIED"
        or str(step.get("tool_name") or "") != expected_tool
    ):
        raise _error("SELECTION_PREVIEW_INVALID", "候选预览没有通过读取验证。")
    result = _mapping_field(step, "result_summary_json", "result_summary")
    digest = str(step.get("result_sha256") or "").strip()
    if (
        str(result.get("status") or "").upper() != "SUCCESS"
        or _HEX_SHA256.fullmatch(digest) is None
        or canonical_sha256(result) != digest
    ):
        raise _error("SELECTION_PREVIEW_INVALID", "候选预览结果校验失败。")

    data = result.get("data")
    meta = result.get("meta")
    if not isinstance(data, Mapping) or not isinstance(meta, Mapping) or data.get("dry_run") is not True:
        raise _error("SELECTION_PREVIEW_INVALID", "候选预览结果格式无效。")
    raw_candidates = data.get("candidates")
    candidate_count = data.get("candidate_count")
    fingerprint = str(data.get("preview_fingerprint") or "").strip()
    if (
        not isinstance(raw_candidates, list)
        or isinstance(candidate_count, bool)
        or not isinstance(candidate_count, int)
        or candidate_count != len(raw_candidates)
        or not 0 <= candidate_count <= _MAX_CANDIDATES
        or _HEX_SHA256.fullmatch(fingerprint) is None
    ):
        raise _error("SELECTION_PREVIEW_INVALID", "候选数量或预览指纹无效。")
    candidates = _validate_candidates(raw_candidates, spec["candidate_fields"])
    observed_at = _parse_timestamp(meta.get("observed_at"), "observed_at")
    checked_now = _aware_utc(now, "now")
    if observed_at > checked_now:
        raise _error("SELECTION_PREVIEW_INVALID", "候选清单的生成时间无效。")
    return _PersistedSelectionPreview(
        run_id=safe_run_id,
        fingerprint=fingerprint,
        candidates=tuple(candidates),
        observed_at=observed_at,
        expires_at=observed_at + SELECTION_PREVIEW_TTL,
        data=dict(data),
    )


def _validate_candidates(rows: list[Any], allowed_fields: frozenset[str]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, Mapping) or set(row) != allowed_fields:
            raise _error("SELECTION_PREVIEW_INVALID", "候选运单字段与签名合同不一致。")
        bill_code = str(row.get("bill_code") or "").strip()
        if not bill_code or len(bill_code) > 64 or bill_code in seen:
            raise _error("SELECTION_PREVIEW_INVALID", "候选运单号缺失或重复。")
        seen.add(bill_code)
        result.append(dict(row))
    return result


def _selected_bill_codes(values: Sequence[str]) -> list[str]:
    if isinstance(values, (str, bytes)) or not values:
        raise _error("SELECTION_REQUIRED", "请至少选择一票运单。")
    selected = [str(item or "").strip() for item in values]
    if any(not item or len(item) > 64 for item in selected) or len(selected) != len(set(selected)):
        raise _error("SELECTION_INVALID", "所选运单号为空、重复或格式无效。")
    return selected


def _summary(automation_id: str, data: Mapping[str, Any]) -> dict[str, Any]:
    if automation_id == "self_pickup_problem_upload":
        return {
            "duplicate_source_rows": int(data.get("duplicate_source_rows") or 0),
        }
    counts = data.get("type_counts") if isinstance(data.get("type_counts"), Mapping) else {}
    return {
        "complete_count": int(data.get("complete_count") or 0),
        "hidden_completed_count": int(data.get("hidden_completed_count") or 0),
        "split_count": int(counts.get("少货/分批") or 0),
        "pending_count": int(counts.get("有发未到") or 0),
    }


def _mapping_field(row: Mapping[str, Any], primary: str, fallback: str) -> dict[str, Any]:
    value = row.get(primary)
    if not isinstance(value, Mapping):
        value = row.get(fallback)
    if not isinstance(value, Mapping):
        raise _error("SELECTION_PREVIEW_INVALID", "持久化运行记录格式无效。")
    return dict(value)


def _parse_timestamp(value: Any, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError as exc:
        raise _error("SELECTION_PREVIEW_INVALID", f"{field} 不是有效时间。") from exc
    return _aware_utc(parsed, field)


def _aware_utc(value: datetime, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise _error("SELECTION_PREVIEW_INVALID", f"{field} 缺少时区。")
    return value.astimezone(timezone.utc)


def _iso_utc(value: datetime) -> str:
    return _aware_utc(value, "timestamp").isoformat().replace("+00:00", "Z")


def _error(code: str, message: str) -> OrchestrationError:
    return OrchestrationError(code, message, details={"status": "BLOCKED_DATA"})


__all__ = [
    "SELECTION_PREVIEW_PROJECTS",
    "SelectionPreviewExpectation",
    "is_selection_preview_project",
    "selection_confirmation_arguments",
    "selection_preview_public_projection",
]
