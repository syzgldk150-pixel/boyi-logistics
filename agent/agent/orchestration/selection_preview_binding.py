"""Persisted preview binding for exact Console/Feishu problem selections."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from agent.orchestration.models import (
    Actor,
    ActorType,
    Command,
    EntityRef,
    OrchestrationError,
    new_id,
)
from agent.orchestration.scan_preview_binding import normalize_preview_run_id
from shared.automation_project_authorization import (
    AutomationProjectInvocation,
    canonical_sha256,
)
from shared.orchestration_repository_support import IdempotencyConflict


SELECTION_PREVIEW_TTL = timedelta(minutes=15)
SELECTION_PREVIEW_CONTEXT_KEY = "selection_preview"
SELECTION_PREVIEW_CONSUMED_EVENT = "automation.selection_preview.consumed"
SELECTION_PREVIEW_CONTRACT_VERSION = 1
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
_MAX_FORMAL_SELECTION = 250
_SERVICE_V2_SELECTION_ENTRYPOINTS = frozenset({"console", "feishu"})


@dataclass(frozen=True)
class SelectionPreviewExpectation:
    project_instance_id: str
    plugin_id: str
    generation: int
    contract_digest: str
    configuration_version: int
    runtime_model: str = "ACTION_V1"
    entrypoint: str | None = None
    contribution_id: str | None = None
    title: str | None = None


@dataclass(frozen=True)
class SelectionPreviewResolution:
    context: Mapping[str, Any]
    formal_arguments: Mapping[str, Any]


@dataclass(frozen=True)
class _PersistedSelectionPreview:
    run_id: str
    step: Mapping[str, Any]
    result_digest: str
    fingerprint: str
    candidates: tuple[Mapping[str, Any], ...]
    observed_at: datetime
    expires_at: datetime
    data: Mapping[str, Any]


def is_selection_preview_project(entry: Any) -> bool:
    automation_id = str(getattr(entry, "automation_id", "") or "").strip()
    spec = SELECTION_PREVIEW_PROJECTS.get(automation_id)
    if bool(
        spec
        and str(getattr(entry, "plugin_id", "") or "").strip()
        == str(spec["plugin_id"])
        and str(getattr(entry, "trust_source", "") or "").strip()
        == "ed25519_first_party"
    ):
        return True
    return any(
        selection_preview_contribution(entry, contribution_kind) is not None
        for contribution_kind in ("console", "feishu")
    )


def selection_preview_contribution(
    entry: Any,
    contribution_kind: str,
) -> Mapping[str, Any] | None:
    """Return one signed Service-v2 selection contribution without guessing."""

    if str(getattr(entry, "runtime_model", "") or "") != "SERVICE_V2":
        return None
    contributions = getattr(entry, "contributions", None)
    declarations = (
        contributions.get(contribution_kind)
        if isinstance(contributions, Mapping)
        else None
    )
    matches = [
        dict(item)
        for item in declarations or ()
        if isinstance(item, Mapping)
        and isinstance(item.get("selection_preview_operation"), str)
        and str(item.get("selection_preview_operation") or "").strip()
    ]
    return matches[0] if len(matches) == 1 else None


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
    spec = SELECTION_PREVIEW_PROJECTS.get(expectation.project_instance_id)
    title = (
        str(expectation.title or "").strip()
        if expectation.runtime_model == "SERVICE_V2"
        else str(spec["title"] if spec is not None else "")
    )
    if not title:
        raise _error("SELECTION_PREVIEW_PROJECT_INVALID", "候选选择标题无效。")
    return {
        "contract_version": 1,
        "automation_id": expectation.project_instance_id,
        "title": title,
        "preview_run_id": persisted.run_id,
        "observed_at": _iso_utc(persisted.observed_at),
        "expires_at": _iso_utc(persisted.expires_at),
        "candidate_count": len(persisted.candidates),
        "candidates": [dict(item) for item in persisted.candidates],
        "summary": (
            _service_v2_summary(persisted.data)
            if expectation.runtime_model == "SERVICE_V2"
            else _summary(expectation.project_instance_id, persisted.data)
        ),
        "can_confirm": _aware_utc(now, "now") < persisted.expires_at,
    }


def selection_confirmation_arguments(
    uow: Any,
    *,
    preview_run_id: str,
    expectation: SelectionPreviewExpectation,
    selected_bill_codes: Sequence[str],
    now: datetime,
    for_update: bool = False,
) -> dict[str, Any]:
    persisted = _load_preview(
        uow,
        preview_run_id=preview_run_id,
        expectation=expectation,
        now=now,
        for_update=for_update,
    )
    checked_now = _aware_utc(now, "now")
    if checked_now >= persisted.expires_at:
        raise _error("SELECTION_PREVIEW_EXPIRED", "候选清单已超过十五分钟，请重新生成。")
    return _formal_selection_arguments(
        persisted,
        selected_bill_codes=selected_bill_codes,
    )


def resolve_selection_preview(
    uow: Any,
    *,
    preview_run_id: str,
    expectation: SelectionPreviewExpectation,
    selected_bill_codes: Sequence[str],
    now: datetime,
    for_update: bool,
) -> SelectionPreviewResolution:
    """Resolve one formal Service-v2 selection and its signed Host context."""

    entrypoint, contribution_id = _service_v2_expectation_identity(expectation)
    checked_now = _aware_utc(now, "now")
    persisted = _load_preview(
        uow,
        preview_run_id=preview_run_id,
        expectation=expectation,
        now=checked_now,
        for_update=for_update,
    )
    if checked_now >= persisted.expires_at:
        raise _error("SELECTION_PREVIEW_EXPIRED", "候选清单已超过十五分钟，请重新生成。")
    formal_arguments = _formal_selection_arguments(
        persisted,
        selected_bill_codes=selected_bill_codes,
    )
    selected = list(formal_arguments["selected_bill_codes"])
    if len(selected) > _MAX_FORMAL_SELECTION:
        raise _error("SELECTION_INVALID", "所选运单号为空、重复或格式无效。")
    candidates = [dict(item) for item in persisted.candidates]
    context: dict[str, Any] = {
        "contract_version": SELECTION_PREVIEW_CONTRACT_VERSION,
        "plugin_id": expectation.plugin_id,
        "preview_run_id": persisted.run_id,
        "preview_step_id": str(persisted.step.get("step_id") or "").strip(),
        "preview_result_sha256": persisted.result_digest,
        "project_instance_id": expectation.project_instance_id,
        "generation": expectation.generation,
        "contract_digest": expectation.contract_digest,
        "configuration_version": expectation.configuration_version,
        "entrypoint": entrypoint,
        "contribution_id": contribution_id,
        "preview_fingerprint": persisted.fingerprint,
        "candidate_count": len(candidates),
        "candidates_sha256": canonical_sha256(candidates),
        "selection_count": len(selected),
        "selection_sha256": canonical_sha256(selected),
        "formal_arguments_sha256": canonical_sha256(formal_arguments),
        "observed_at": _iso_utc(persisted.observed_at),
        "expires_at": _iso_utc(persisted.expires_at),
    }
    context["context_sha256"] = canonical_sha256(context)
    return SelectionPreviewResolution(
        context=context,
        formal_arguments=formal_arguments,
    )


def _formal_selection_arguments(
    persisted: _PersistedSelectionPreview,
    *,
    selected_bill_codes: Sequence[str],
) -> dict[str, Any]:
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
    for_update: bool = False,
) -> _PersistedSelectionPreview:
    safe_run_id = normalize_preview_run_id(preview_run_id)
    spec = SELECTION_PREVIEW_PROJECTS.get(expectation.project_instance_id)
    is_service_v2 = expectation.runtime_model == "SERVICE_V2"
    if not is_service_v2 and (
        spec is None or expectation.plugin_id != spec["plugin_id"]
    ):
        raise _error("SELECTION_PREVIEW_PROJECT_INVALID", "该项目不支持后台候选选择。")
    service_v2_identity = (
        _service_v2_expectation_identity(expectation) if is_service_v2 else None
    )
    run = uow.runs.get(safe_run_id, for_update=for_update)
    if run is None:
        raise _error("SELECTION_PREVIEW_NOT_FOUND", "没有找到本次候选清单。")
    if str(run.get("status") or "") != "COMPLETED":
        raise _error("SELECTION_PREVIEW_INCOMPLETE", "候选清单尚未生成完成。")

    command_id = str(run.get("command_id") or "").strip()
    command = uow.commands.get(command_id, for_update=False) if command_id else None
    if command is None or str(command.get("command_type") or "") != "automation.project.invoke":
        raise _error("SELECTION_PREVIEW_INVALID", "候选清单没有可信运行记录。")
    invocation = _mapping_field(command, "automation_invocation_json", "automation_invocation")
    expected_entrypoint = service_v2_identity[0] if service_v2_identity else None
    expected_contribution_id = service_v2_identity[1] if service_v2_identity else None
    if (
        str(invocation.get("automation_id") or "") != expectation.project_instance_id
        or int(invocation.get("automation_generation") or 0) != expectation.generation
        or str(invocation.get("contract_hash") or "") != expectation.contract_digest
        or int(invocation.get("project_configuration_version") or 0)
        != expectation.configuration_version
        or (
            is_service_v2
            and (
                str(command.get("source") or "") != expected_entrypoint
                or str(invocation.get("entrypoint") or "") != expected_entrypoint
                or str(invocation.get("contract_id") or "")
                != expected_contribution_id
            )
        )
    ):
        raise _error("SELECTION_PREVIEW_STALE", "项目配置已变化，请重新生成候选清单。")

    parameters = _mapping_field(command, "parameters_json", "parameters")
    expected_tool = f"automation.{expectation.project_instance_id}.run"
    arguments = parameters.get("arguments")
    execution_context = parameters.get("execution_context")
    if (
        str(parameters.get("tool_name") or "") != expected_tool
        or not isinstance(arguments, Mapping)
        or arguments.get("dry_run") is not True
        or arguments.get("selected_bill_codes") not in (None, [])
        or str(arguments.get("preview_fingerprint") or "")
        or (
            is_service_v2
            and (
                not isinstance(execution_context, Mapping)
                or execution_context.get("selection_phase") != "PREVIEW"
            )
        )
    ):
        raise _error("SELECTION_PREVIEW_INVALID", "运行记录不是只读候选预览。")

    steps = uow.steps.list_for_run(safe_run_id)
    if len(steps) != 1:
        raise _error("SELECTION_PREVIEW_INVALID", "候选预览的运行步骤不完整。")
    step = steps[0]
    if (
        not str(step.get("step_id") or "").strip()
        or str(step.get("status") or "") != "COMPLETED"
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
    candidates = (
        _validate_generic_candidates(raw_candidates)
        if is_service_v2
        else _validate_candidates(raw_candidates, spec["candidate_fields"])
    )
    observed_at = _parse_timestamp(meta.get("observed_at"), "observed_at")
    checked_now = _aware_utc(now, "now")
    if observed_at > checked_now:
        raise _error("SELECTION_PREVIEW_INVALID", "候选清单的生成时间无效。")
    return _PersistedSelectionPreview(
        run_id=safe_run_id,
        step=dict(step),
        result_digest=digest,
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


def _validate_generic_candidates(rows: list[Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        if (
            not isinstance(row, Mapping)
            or not 1 <= len(row) <= 32
            or "bill_code" not in row
            or any(
                not isinstance(key, str)
                or not key
                or len(key) > 64
                or any(
                    token in key.lower()
                    for token in ("password", "cookie", "credential", "secret", "token")
                )
                for key in row
            )
            or any(
                isinstance(value, (Mapping, list, tuple, set, bytes))
                or (isinstance(value, str) and len(value) > 1000)
                for value in row.values()
            )
        ):
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
    if any(not item or len(item) > 64 for item in selected) or len(selected) != len(
        set(selected)
    ):
        raise _error("SELECTION_INVALID", "所选运单号为空、重复或格式无效。")
    return selected


def _service_v2_expectation_identity(
    expectation: SelectionPreviewExpectation,
) -> tuple[str, str]:
    entrypoint = str(expectation.entrypoint or "").strip()
    contribution_id = str(expectation.contribution_id or "").strip()
    if (
        expectation.runtime_model != "SERVICE_V2"
        or entrypoint not in _SERVICE_V2_SELECTION_ENTRYPOINTS
        or not contribution_id
        or len(contribution_id) > 128
        or not str(expectation.project_instance_id or "").strip()
        or not str(expectation.plugin_id or "").strip()
        or isinstance(expectation.generation, bool)
        or not isinstance(expectation.generation, int)
        or expectation.generation <= 0
        or _HEX_SHA256.fullmatch(str(expectation.contract_digest or "")) is None
        or isinstance(expectation.configuration_version, bool)
        or not isinstance(expectation.configuration_version, int)
        or expectation.configuration_version <= 0
    ):
        raise _error(
            "SELECTION_PREVIEW_PROJECT_INVALID",
            "Service v2 selection contribution is incomplete or ambiguous",
        )
    return entrypoint, contribution_id


def validate_selection_preview_context(value: Any) -> dict[str, Any]:
    """Validate the compact Host-owned context persisted on a formal command."""

    if not isinstance(value, Mapping):
        raise _error(
            "SELECTION_PREVIEW_CONTEXT_INVALID",
            "Selection preview context must be an object",
        )
    context = dict(value)
    expected_fields = {
        "contract_version",
        "plugin_id",
        "preview_run_id",
        "preview_step_id",
        "preview_result_sha256",
        "project_instance_id",
        "generation",
        "contract_digest",
        "configuration_version",
        "entrypoint",
        "contribution_id",
        "preview_fingerprint",
        "candidate_count",
        "candidates_sha256",
        "selection_count",
        "selection_sha256",
        "formal_arguments_sha256",
        "observed_at",
        "expires_at",
        "context_sha256",
    }
    if set(context) != expected_fields:
        raise _error(
            "SELECTION_PREVIEW_CONTEXT_INVALID",
            "Selection preview context schema is invalid",
        )
    supplied_digest = _context_digest(context.get("context_sha256"), "context_sha256")
    unhashed = dict(context)
    unhashed.pop("context_sha256")
    if canonical_sha256(unhashed) != supplied_digest:
        raise _error(
            "SELECTION_PREVIEW_CONTEXT_INVALID",
            "Selection preview context digest is stale",
        )
    if context.get("contract_version") != SELECTION_PREVIEW_CONTRACT_VERSION:
        raise _error(
            "SELECTION_PREVIEW_CONTEXT_INVALID",
            "Selection preview context version is unsupported",
        )
    normalized_run_id = normalize_preview_run_id(context.get("preview_run_id"))
    if context.get("preview_run_id") != normalized_run_id:
        raise _error(
            "SELECTION_PREVIEW_CONTEXT_INVALID",
            "Selection preview run identity is not canonical",
        )
    for name, maximum in (
        ("plugin_id", 128),
        ("preview_step_id", 64),
        ("project_instance_id", 128),
        ("contribution_id", 128),
    ):
        _context_text(context.get(name), name, maximum=maximum)
    if context.get("entrypoint") not in _SERVICE_V2_SELECTION_ENTRYPOINTS:
        raise _error(
            "SELECTION_PREVIEW_CONTEXT_INVALID",
            "Selection preview entrypoint is invalid",
        )
    for name in (
        "preview_result_sha256",
        "contract_digest",
        "preview_fingerprint",
        "candidates_sha256",
        "selection_sha256",
        "formal_arguments_sha256",
    ):
        _context_digest(context.get(name), name)
    _context_positive_int(context.get("generation"), "generation")
    _context_positive_int(
        context.get("configuration_version"),
        "configuration_version",
    )
    candidate_count = _context_bounded_int(
        context.get("candidate_count"),
        "candidate_count",
        0,
        _MAX_CANDIDATES,
    )
    selection_count = _context_bounded_int(
        context.get("selection_count"),
        "selection_count",
        1,
        _MAX_FORMAL_SELECTION,
    )
    if selection_count > candidate_count:
        raise _error(
            "SELECTION_PREVIEW_CONTEXT_INVALID",
            "Selection preview counts are inconsistent",
        )
    observed_at = _parse_timestamp(context.get("observed_at"), "observed_at")
    expires_at = _parse_timestamp(context.get("expires_at"), "expires_at")
    if expires_at != observed_at + SELECTION_PREVIEW_TTL:
        raise _error(
            "SELECTION_PREVIEW_CONTEXT_INVALID",
            "Selection preview expiry is invalid",
        )
    return context


def ensure_selection_preview_active(value: Any, *, now: datetime) -> None:
    """Recheck expiry after the caller acquires the preview Run lock."""

    context = validate_selection_preview_context(value)
    checked_now = _aware_utc(now, "now")
    if checked_now >= _parse_timestamp(context["expires_at"], "expires_at"):
        raise _error("SELECTION_PREVIEW_EXPIRED", "候选清单已超过十五分钟，请重新生成。")


def restore_selection_preview_replay(
    uow: Any,
    *,
    source: str,
    idempotency_key: str,
    actor: Actor,
    trusted_context: Mapping[str, Any],
    project_instance_id: str,
    request_id: str,
    preview_run_id: str,
    selected_bill_codes: Sequence[str],
    expected_entrypoint: str | None = None,
    expected_contribution_id: str | None = None,
    expected_generation: int | None = None,
    expected_configuration_version: int | None = None,
) -> Command | None:
    """Restore the exact accepted formal Command, including after preview expiry."""

    persisted = uow.commands.get_by_idempotency(
        source,
        idempotency_key,
        for_update=False,
    )
    if persisted is None:
        return None
    try:
        safe_entrypoint = str(expected_entrypoint or source).strip()
        expected_contribution = str(expected_contribution_id or "").strip() or None
        selected = _selected_bill_codes(selected_bill_codes)
        if (
            safe_entrypoint not in _SERVICE_V2_SELECTION_ENTRYPOINTS
            or safe_entrypoint != source
            or str(persisted.get("command_type") or "")
            != "automation.project.invoke"
            or str(persisted.get("source") or "") != source
            or str(persisted.get("idempotency_key") or "") != idempotency_key
            or str(persisted.get("actor_type") or "") != actor.actor_type.value
            or str(persisted.get("actor_id") or "") != actor.actor_id
        ):
            raise ValueError("persisted command identity differs")
        roles = persisted.get("actor_roles_json", persisted.get("actor_roles"))
        if not isinstance(roles, list) or tuple(sorted(roles)) != actor.roles:
            raise ValueError("persisted command actor differs")

        parameters = _mapping_field(persisted, "parameters_json", "parameters")
        execution_context = parameters.get("execution_context")
        if not isinstance(execution_context, Mapping):
            raise ValueError("persisted execution context is invalid")
        preview_context = validate_selection_preview_context(
            execution_context.get(SELECTION_PREVIEW_CONTEXT_KEY)
        )
        if (
            preview_context["preview_run_id"]
            != normalize_preview_run_id(preview_run_id)
            or preview_context["project_instance_id"] != project_instance_id
            or preview_context["entrypoint"] != safe_entrypoint
            or (
                expected_contribution is not None
                and preview_context["contribution_id"] != expected_contribution
            )
        ):
            raise ValueError("persisted preview identity differs")

        arguments = parameters.get("arguments")
        if not isinstance(arguments, Mapping):
            raise ValueError("persisted formal arguments are invalid")
        if (
            str(parameters.get("tool_name") or "")
            != f"automation.{project_instance_id}.run"
            or arguments.get("dry_run") is not False
            or arguments.get("selected_bill_codes") != selected
            or arguments.get("preview_fingerprint")
            != preview_context["preview_fingerprint"]
            or canonical_sha256(arguments)
            != preview_context["formal_arguments_sha256"]
            or canonical_sha256(selected) != preview_context["selection_sha256"]
            or len(selected) != preview_context["selection_count"]
        ):
            raise ValueError("persisted formal selection differs")

        supplied_dynamic_inputs = trusted_context.get("dynamic_inputs")
        if supplied_dynamic_inputs is not None and supplied_dynamic_inputs != arguments:
            raise ValueError("requested selection differs")
        persisted_dynamic_inputs = execution_context.get("dynamic_inputs")
        if persisted_dynamic_inputs is not None and persisted_dynamic_inputs != arguments:
            raise ValueError("persisted selection inputs differ")
        if (
            execution_context.get("project_request_id") != request_id
            or execution_context.get("entrypoint") != safe_entrypoint
            or execution_context.get("contribution_id")
            != preview_context["contribution_id"]
            or execution_context.get("selection_phase") != "FORMAL"
            or execution_context.get("occurred_at") != preview_context["observed_at"]
            or _selection_transport_context(execution_context)
            != _selection_transport_context(trusted_context)
        ):
            raise ValueError("persisted trusted transport context differs")

        invocation = AutomationProjectInvocation.from_mapping(
            _mapping_field(
                persisted,
                "automation_invocation_json",
                "automation_invocation",
            )
        )
        if (
            invocation.automation_id != project_instance_id
            or invocation.request_id != request_id
            or invocation.entrypoint.value != safe_entrypoint
            or invocation.contract_id != preview_context["contribution_id"]
            or invocation.automation_generation != preview_context["generation"]
            or invocation.contract_hash != preview_context["contract_digest"]
            or invocation.project_configuration_version
            != preview_context["configuration_version"]
        ):
            raise ValueError("persisted project invocation differs")
        if (
            expected_generation is not None
            and invocation.automation_generation != expected_generation
        ) or (
            expected_configuration_version is not None
            and invocation.project_configuration_version
            != expected_configuration_version
        ):
            raise OrchestrationError(
                "PROJECT_INVOCATION_STALE",
                "Automation project generation or configuration changed before replay",
            )

        raw_refs = persisted.get("entity_refs_json", persisted.get("entity_refs"))
        if not isinstance(raw_refs, list):
            raise ValueError("persisted entity references are invalid")
        refs = tuple(
            EntityRef(
                entity_type=str(ref["entity_type"]),
                entity_id=str(ref["entity_id"]),
                source_system=str(ref.get("source_system") or ""),
                relation_type=str(ref.get("relation_type") or "subject"),
                metadata=dict(ref.get("metadata") or {}),
            )
            for ref in raw_refs
            if isinstance(ref, Mapping)
        )
        if len(refs) != len(raw_refs):
            raise ValueError("persisted entity references are invalid")
        requested_at = persisted.get("requested_at")
        if not isinstance(requested_at, datetime):
            raise ValueError("persisted request time is invalid")
        if requested_at.tzinfo is None:
            requested_at = requested_at.replace(tzinfo=timezone.utc)
        return Command(
            command_type="automation.project.invoke",
            source=source,
            actor=Actor(
                ActorType(str(persisted["actor_type"])),
                str(persisted["actor_id"]),
                tuple(roles),
            ),
            parameters=parameters,
            idempotency_key=idempotency_key,
            entity_refs=refs,
            automation_invocation=invocation,
            command_id=str(persisted["command_id"]),
            correlation_id=str(persisted["correlation_id"]),
            requested_at=requested_at.astimezone(timezone.utc),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise OrchestrationError(
            "REQUEST_ID_REUSED",
            "Request id was already used for a different selection command",
        ) from exc


def _selection_transport_context(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("trusted context is invalid")
    server_owned = {
        "project_request_id",
        "entrypoint",
        "occurred_at",
        "contribution_id",
        "selection_phase",
        "dynamic_inputs",
        SELECTION_PREVIEW_CONTEXT_KEY,
    }
    return {name: item for name, item in value.items() if name not in server_owned}


def consume_selection_preview(
    uow: Any,
    *,
    expectation: SelectionPreviewExpectation,
    context: Mapping[str, Any] | None,
    command: Command,
    occurred_at: datetime,
) -> None:
    """Consume one Service-v2 preview in the Command-acceptance transaction."""

    if expectation.runtime_model != "SERVICE_V2":
        return
    entrypoint, contribution_id = _service_v2_expectation_identity(expectation)
    validated = validate_selection_preview_context(context)
    if (
        validated["plugin_id"] != expectation.plugin_id
        or validated["project_instance_id"] != expectation.project_instance_id
        or validated["generation"] != expectation.generation
        or validated["contract_digest"] != expectation.contract_digest
        or validated["configuration_version"] != expectation.configuration_version
        or validated["entrypoint"] != entrypoint
        or validated["contribution_id"] != contribution_id
        or command.source != entrypoint
    ):
        raise _error(
            "SELECTION_PREVIEW_CONTEXT_INVALID",
            "Selection preview context does not match the signed command",
        )
    existing = uow.commands.get_by_idempotency(
        command.source,
        command.idempotency_key,
        for_update=True,
    )
    if existing is not None:
        return
    event_time = _aware_utc(occurred_at, "occurred_at").replace(tzinfo=None)
    try:
        uow.events.append_with_outbox(
            {
                "event_id": new_id(),
                "event_type": SELECTION_PREVIEW_CONSUMED_EVENT,
                "schema_version": 1,
                "source_system": "automation_project",
                "source_event_id": validated["preview_run_id"],
                "entity_type": "agent_run",
                "entity_id": validated["preview_run_id"],
                "run_id": validated["preview_run_id"],
                "step_id": validated["preview_step_id"],
                "occurred_at": event_time,
                "observed_at": event_time,
                "correlation_id": command.correlation_id,
                "causation_id": command.command_id,
                "payload": {
                    "command_source": command.source,
                    "command_idempotency_key": command.idempotency_key,
                    "context_sha256": validated["context_sha256"],
                    "selection_count": validated["selection_count"],
                    "selection_sha256": validated["selection_sha256"],
                },
            },
            (),
        )
    except IdempotencyConflict as exc:
        raise _error(
            "SELECTION_PREVIEW_ALREADY_CONSUMED",
            "候选清单已由另一个请求确认，请重新生成。",
        ) from exc


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


def _service_v2_summary(data: Mapping[str, Any]) -> dict[str, Any]:
    allowed = (
        "duplicate_source_rows",
        "complete_count",
        "hidden_completed_count",
    )
    result: dict[str, Any] = {}
    for field in allowed:
        value = data.get(field)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            continue
        result[field] = value
    counts = data.get("type_counts")
    if isinstance(counts, Mapping) and all(
        isinstance(key, str)
        and key
        and isinstance(value, int)
        and not isinstance(value, bool)
        and value >= 0
        for key, value in counts.items()
    ):
        result["type_counts"] = dict(counts)
    return result


def _context_text(value: Any, name: str, *, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise _error(
            "SELECTION_PREVIEW_CONTEXT_INVALID",
            f"Selection preview {name} is invalid",
        )
    return value.strip()


def _context_digest(value: Any, name: str) -> str:
    text = str(value or "").strip()
    if _HEX_SHA256.fullmatch(text) is None:
        raise _error(
            "SELECTION_PREVIEW_CONTEXT_INVALID",
            f"Selection preview {name} is invalid",
        )
    return text


def _context_positive_int(value: Any, name: str) -> int:
    return _context_bounded_int(value, name, 1, 2_147_483_647)


def _context_bounded_int(
    value: Any,
    name: str,
    minimum: int,
    maximum: int,
) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not minimum <= value <= maximum
    ):
        raise _error(
            "SELECTION_PREVIEW_CONTEXT_INVALID",
            f"Selection preview {name} is invalid",
        )
    return value


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
    "SELECTION_PREVIEW_CONTEXT_KEY",
    "SELECTION_PREVIEW_CONSUMED_EVENT",
    "SELECTION_PREVIEW_PROJECTS",
    "SelectionPreviewExpectation",
    "SelectionPreviewResolution",
    "consume_selection_preview",
    "ensure_selection_preview_active",
    "is_selection_preview_project",
    "resolve_selection_preview",
    "restore_selection_preview_replay",
    "selection_preview_contribution",
    "selection_confirmation_arguments",
    "selection_preview_public_projection",
    "validate_selection_preview_context",
]
