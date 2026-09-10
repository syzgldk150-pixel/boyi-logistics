"""Exact preview selection bound to real Invocation rows, without Run aliases."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Mapping
import uuid

from agent.orchestration.models import OrchestrationError
from agent.orchestration.scan_preview_binding import _validate_preview_evidence, _bind_formal_arguments, validate_scan_preview_context
from agent.orchestration.selection_preview_binding import SELECTION_PREVIEW_PROJECTS, _validate_candidates, _validate_generic_candidates, _selected_bill_codes, _summary, _service_v2_summary
from shared.automation_project_authorization import canonical_sha256


def _load(repository, invocation_id, *, entry, contract, actor_id=None):
    try:
        if str(uuid.UUID(invocation_id)) != invocation_id:
            raise ValueError("not canonical")
    except (TypeError, ValueError, AttributeError) as exc:
        raise OrchestrationError("PREVIEW_ID_INVALID", "候选清单标识无效") from exc
    row = repository.get(invocation_id)
    if not row or row["status"] != "COMPLETED":
        raise OrchestrationError("PREVIEW_INCOMPLETE", "本次候选清单尚未读取成功")
    invocation = row["invocation_json"]
    if (row["automation_id"] != entry.automation_id or row["generation"] != contract.automation_generation
            or invocation["contract_hash"] != contract.contract_hash
            or invocation["project_configuration_version"] != contract.project_configuration_version
            or (actor_id is not None and row["actor_id"] != actor_id)):
        raise OrchestrationError("PREVIEW_STALE", "候选清单的账号、设置或插件版本已变化，请重新读取")
    result, arguments = row["result_json"], row["arguments_json"]
    if not isinstance(result, Mapping) or result.get("status") != "SUCCESS" or arguments.get("dry_run") is not True:
        raise OrchestrationError("PREVIEW_INVALID", "本次结果不是已核验的只读预览")
    data = result.get("data")
    if not isinstance(data, Mapping) or data.get("dry_run") is not True:
        raise OrchestrationError("PREVIEW_INVALID", "预览数据格式无效")
    observed = datetime.fromisoformat(result["meta"]["observed_at"].replace("Z", "+00:00"))
    now = datetime.now(timezone.utc)
    if observed.tzinfo is None or observed > now:
        raise OrchestrationError("PREVIEW_INVALID", "预览时间无效")
    return row, result, data, observed, observed + timedelta(minutes=15)


def _candidates(entry, data):
    rows = data.get("candidates")
    fingerprint = data.get("preview_fingerprint")
    if (not isinstance(rows, list) or type(data.get("candidate_count")) is not int
            or len(rows) != data["candidate_count"] or len(rows) > 10000
            or not isinstance(fingerprint, str) or len(fingerprint) != 64
            or any(char not in "0123456789abcdef" for char in fingerprint)):
        raise OrchestrationError("PREVIEW_INVALID", "候选数量或指纹无效")
    if entry.runtime_model == "SERVICE_V2":
        return _validate_generic_candidates(rows)
    spec = SELECTION_PREVIEW_PROJECTS[entry.automation_id]
    return _validate_candidates(rows, spec["candidate_fields"])


def project_preview(repository, invocation_id, *, entry, contract, scan):
    row, result, data, observed, expires = _load(repository, invocation_id, entry=entry, contract=contract)
    projection = {"contract_version": 2, "preview_invocation_id": invocation_id, "automation_id": entry.automation_id, "observed_at": observed.isoformat(), "expires_at": expires.isoformat(), "can_confirm": datetime.now(timezone.utc) < expires and not row.get("preview_consumed_by")}
    if scan:
        evidence = _validate_preview_evidence(data.get("preview_evidence", {}), row["arguments_json"])
        projection.update({key: evidence[key] for key in ("target_date", "source_page_count", "normalized_record_count", "selection_count", "batch_count")})
    else:
        candidates = _candidates(entry, data)
        projection.update(title=entry.display_name, candidates=candidates, candidate_count=len(candidates), summary=_service_v2_summary(data) if entry.runtime_model == "SERVICE_V2" else _summary(entry.automation_id, data))
    return projection


def confirm_preview(repository, invocation_id, *, entry, contract, actor_id, arguments, selected_bill_codes, scan):
    row, result, data, observed, expires = _load(repository, invocation_id, entry=entry, contract=contract, actor_id=actor_id)
    if datetime.now(timezone.utc) >= expires:
        raise OrchestrationError("PREVIEW_EXPIRED", "候选清单已过期，请重新读取")
    if scan:
        if selected_bill_codes is not None:
            raise OrchestrationError("PREVIEW_INPUT_INVALID", "扫描确认不接受替换单号")
        evidence = _validate_preview_evidence(data.get("preview_evidence", {}), row["arguments_json"])
        formal = _bind_formal_arguments(row["arguments_json"], arguments, target_date=evidence["target_date"])
        context = {"contract_version": 2, "plugin_id": entry.plugin_id,
            "preview_invocation_id": invocation_id, "preview_result_sha256": canonical_sha256(result),
            "project_instance_id": entry.automation_id, "generation": contract.automation_generation,
            "contract_digest": contract.contract_hash, "configuration_version": contract.project_configuration_version,
            "observed_at": observed.isoformat(), "expires_at": expires.isoformat(),
            "source_evidence_count": len(evidence["source_evidence_refs"]),
            "source_evidence_refs_sha256": canonical_sha256(evidence["source_evidence_refs"]),
            "formal_arguments_sha256": canonical_sha256(formal)}
        context.update({key: evidence[key] for key in ("target_date", "source_page_count", "normalized_record_count", "source_snapshot_sha256", "selection_count", "selection_sha256", "batch_count", "batch_plan_sha256")})
        context["context_sha256"] = canonical_sha256(context)
        formal["_scan_preview_binding"] = validate_scan_preview_context(context)
        return formal
    candidates = _candidates(entry, data)
    selection_fields = {"dry_run", "selected_bill_codes", "preview_fingerprint"}
    if {key: value for key, value in row["arguments_json"].items() if key not in selection_fields} != {key: value for key, value in arguments.items() if key not in selection_fields}:
        raise OrchestrationError("PREVIEW_STALE", "正式执行条件与候选清单不同，请重新读取")
    selected = _selected_bill_codes(selected_bill_codes or ())
    if not selected or len(selected) > (90 if entry.plugin_id == "split_pending_problem_upload" else 250):
        raise OrchestrationError("SELECTION_INPUT_INVALID", "请选择范围内的候选运单")
    available = {item["bill_code"] for item in candidates}
    if not set(selected) <= available:
        raise OrchestrationError("SELECTION_INPUT_INVALID", "所选运单不属于本次候选清单")
    return {**arguments, "dry_run": False, "selected_bill_codes": selected, "preview_fingerprint": data["preview_fingerprint"]}
